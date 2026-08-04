from gjp_common.paths import discover_project_root, resolve_input_path, resolve_output_path


def _project(tmp_path):
    root = tmp_path / "project"
    (root / "src" / "gjp_common").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "out" / "scenarios").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    return root


def test_project_root_is_discovered_from_nested_docs_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("GJP_PROJECT_ROOT", raising=False)
    root = _project(tmp_path)

    assert discover_project_root(root / "docs") == root


def test_relative_input_and_output_paths_are_project_root_relative(tmp_path, monkeypatch):
    monkeypatch.delenv("GJP_PROJECT_ROOT", raising=False)
    root = _project(tmp_path)
    template = root / "out" / "scenarios" / "template.json"
    template.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(root / "docs")

    assert resolve_input_path("out/scenarios/template.json", root) == template
    assert resolve_output_path("out/generated.json", root) == root / "out" / "generated.json"
