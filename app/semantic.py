from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


POSITIVE_OPPORTUNITY_PROTOTYPES = (
    "A lasting change in daily behavior creates a new physical-use scenario and an unmet need for a practical consumer product.",
    "New environmental, regulatory, or lifestyle constraints make existing physical consumer products inconvenient or insufficient.",
    "Consumers repeatedly describe a concrete problem that can be addressed by a shippable, low-risk physical product.",
)

NEGATIVE_OPPORTUNITY_PROTOTYPES = (
    "Celebrity gossip, sports results, or entertainment news with no lasting consumer behavior change.",
    "A tragedy, crime, medical claim, dangerous activity, or copyrighted event that should not be commercialized.",
    "A software, subscription, course, consulting service, or information product with no physical consumer product opportunity.",
)

CATEGORY_PROTOTYPES = {
    "家居收纳": "physical home organization, storage, cleaning and small-space household products",
    "出行户外": "physical travel, commuting, weather preparation, camping and outdoor accessories",
    "厨房餐饮": "physical kitchen tools, food storage, meal preparation and reusable dining products",
    "宠物用品": "physical pet care, cleaning, feeding, transport and observation products",
    "个护整理": "physical personal care organization, reusable containers and non-medical grooming tools",
    "汽车配件": "physical automotive organization, charging, maintenance and in-car convenience accessories",
}


class EmbeddingUnavailable(RuntimeError):
    pass


class TextEmbedder(Protocol):
    model_id: str
    model_version: str

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


class SentenceTransformerEmbedder:
    """Lazy local-first adapter; importing this module never imports torch."""

    def __init__(
        self,
        model_id: str,
        model_version: str = "main",
        cache_dir: Path = Path("data/models"),
        local_files_only: bool = True,
    ):
        self.model_id = model_id
        self.model_version = model_version
        self.cache_dir = cache_dir
        self.local_files_only = local_files_only
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingUnavailable(
                "sentence-transformers is not installed; install the optional ml extra"
            ) from exc
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._model = SentenceTransformer(
                self.model_id,
                revision=self.model_version,
                cache_folder=str(self.cache_dir),
                local_files_only=self.local_files_only,
            )
        except Exception as exc:
            mode = "local cache" if self.local_files_only else "configured model source"
            raise EmbeddingUnavailable(f"embedding model unavailable from {mode}: {exc}") from exc
        return self._model

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._load()
        values = model.encode(list(texts), normalize_embeddings=True)
        return [[float(item) for item in vector] for vector in values]


@dataclass(slots=True)
class SemanticFeatureResult:
    embedding: list[float]
    category_matches: list[dict[str, float | str]]
    positive_similarity: float
    negative_similarity: float
    opportunity_similarity: float


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def semantic_input(title: str, evidence_excerpts: Sequence[str]) -> str:
    evidence = " ".join(value.strip()[:600] for value in evidence_excerpts if value.strip())
    return f"{title.strip()}\n{evidence}".strip()


def semantic_input_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SemanticFeatureExtractor:
    def __init__(self, embedder: TextEmbedder):
        self.embedder = embedder

    def extract(self, text: str, category_limit: int = 3) -> SemanticFeatureResult:
        prototype_texts = [
            *POSITIVE_OPPORTUNITY_PROTOTYPES,
            *NEGATIVE_OPPORTUNITY_PROTOTYPES,
            *CATEGORY_PROTOTYPES.values(),
        ]
        inputs = [text, *prototype_texts]
        if "e5" in self.embedder.model_id.casefold():
            inputs = [f"query: {text}", *[f"passage: {item}" for item in prototype_texts]]
        vectors = self.embedder.encode(inputs)
        event_vector = vectors[0]
        positive_count = len(POSITIVE_OPPORTUNITY_PROTOTYPES)
        negative_count = len(NEGATIVE_OPPORTUNITY_PROTOTYPES)
        positive_vectors = vectors[1 : 1 + positive_count]
        negative_vectors = vectors[1 + positive_count : 1 + positive_count + negative_count]
        category_vectors = vectors[1 + positive_count + negative_count :]
        positive = max(cosine_similarity(event_vector, item) for item in positive_vectors)
        negative = max(cosine_similarity(event_vector, item) for item in negative_vectors)
        category_matches = sorted(
            (
                {"category": category, "similarity": round(cosine_similarity(event_vector, vector), 4)}
                for category, vector in zip(CATEGORY_PROTOTYPES, category_vectors, strict=True)
            ),
            key=lambda item: float(item["similarity"]),
            reverse=True,
        )[:category_limit]
        return SemanticFeatureResult(
            embedding=event_vector,
            category_matches=category_matches,
            positive_similarity=round(positive, 4),
            negative_similarity=round(negative, 4),
            opportunity_similarity=round(positive - negative, 4),
        )


def opportunity_precision_at_k(
    ranked_event_ids: Sequence[int], positive_event_ids: set[int], k: int
) -> float:
    if k <= 0:
        raise ValueError("k must be positive")
    selected = list(ranked_event_ids[:k])
    if not selected:
        return 0.0
    return round(sum(event_id in positive_event_ids for event_id in selected) / len(selected), 4)


def duplicate_rate(cluster_ids: Sequence[int]) -> float:
    if not cluster_ids:
        return 0.0
    return round(1 - len(set(cluster_ids)) / len(cluster_ids), 4)
