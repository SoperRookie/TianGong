"""前端静态资源约束：CSP 不含 unsafe-eval，本地托管的脑图内核（kity / kityminder）不得使用 eval / new Function。"""

from pathlib import Path

VENDOR = Path(__file__).resolve().parents[1] / "app" / "web" / "vendor"


def test_vendor_scripts_no_eval():
    for js in sorted(VENDOR.glob("*.js")):
        text = js.read_text(encoding="utf-8")
        assert "eval(" not in text, f"{js.name} 使用 eval，会被 CSP script-src（无 unsafe-eval）拦截导致脑图内核加载失败"
        assert "new Function(" not in text, f"{js.name} 使用 new Function，会被 CSP 拦截"


def test_csp_has_no_unsafe_eval():
    main = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert "unsafe-eval" not in main
