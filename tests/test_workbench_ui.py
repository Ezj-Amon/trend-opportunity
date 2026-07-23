from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"


def read_template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def test_default_templates_compile() -> None:
    environment = Environment(loader=FileSystemLoader(TEMPLATES))
    for name in (
        "base.html",
        "dashboard.html",
        "event.html",
        "workbench_queue.html",
        "workbench_item.html",
    ):
        environment.get_template(name)


def test_default_navigation_only_exposes_judgment_flow() -> None:
    base = read_template("base.html")
    nav = re.search(r'<nav class="topnav".*?</nav>', base, re.DOTALL)
    assert nav is not None
    hrefs = re.findall(r'href="([^"]+)"', nav.group(0))
    assert hrefs == ["/", "/workbench", "/workbench/processed"]
    assert 'class="skip-link"' in base
    assert 'aria-current="page"' in base


def test_event_page_has_no_legacy_downstream_ui() -> None:
    event = read_template("event.html")
    for legacy_text in (
        "创建商品方向",
        "商品假设（旧版）",
        "进入商品方向工作台",
        "平台查询词",
        "已确认机会",
    ):
        assert legacy_text not in event
    assert "已采用的关键证据" in event
    assert "初筛结果" in event


def test_judgment_page_exposes_accessible_review_and_supplement_forms() -> None:
    item = read_template("workbench_item.html")
    assert 'aria-label="判断进度"' in item
    assert 'name="fact_check"' in item
    assert 'name="problem_check"' in item
    assert 'name="durability_check"' in item
    assert 'id="review-error"' in item and 'aria-live="assertive"' in item
    assert 'id="supplement-error"' in item
    assert "addressed_missing_evidence" in item
    assert "/api/workbench/research-candidates/${itemId}/evidence" in item


def test_mobile_and_reduced_motion_rules_are_present() -> None:
    css = (ROOT / "app" / "static" / "app.css").read_text(encoding="utf-8")
    assert "@media(prefers-reduced-motion:reduce)" in css
    assert ".topnav{display:flex!important" in css
    assert "min-height:44px" in css
    assert ":focus-visible" in css
