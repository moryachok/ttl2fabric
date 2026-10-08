import argparse
import csv
import json

import pytest
from rdflib import Graph
from rdflib.namespace import OWL, RDF

from ttl2fabric.cli import build_parser, main

# Flags of 'convert' that only matter for the TMDL definition, so 'convert-ttl' does not take them.
TMDL_ONLY = {"--format", "--workspace-id", "--workspace-name", "--lakehouse-id", "--lakehouse-name", "--sql-endpoint"}


def run_ttl(tmp_path, fixtures_dir, *extra, command="convert-ttl", out="out"):
    out_dir = tmp_path / out
    code = main(
        [
            command,
            str(fixtures_dir / "mini.ttl"),
            "--name",
            "TestOnto",
            "--output-dir",
            str(out_dir),
            "--catalog-file",
            str(fixtures_dir / "catalog.json"),
            "--schema",
            "bronze",
            "--log-level",
            "WARNING",
            *extra,
        ]
    )
    return code, out_dir


def subcommand_options(name):
    sub = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    return {opt for action in sub.choices[name]._actions for opt in action.option_strings}


def test_writes_ttl_and_reports_but_no_definition(tmp_path, fixtures_dir):
    code, out = run_ttl(tmp_path, fixtures_dir, "--strict")
    assert code == 0
    for name in ["TestOnto.ttl", "report.json", "skipped.csv", "skipped.jsonl", "findings.csv", "ttl2fabric.log"]:
        assert (out / name).exists(), name
    assert not (out / "definition").exists() and not (out / "envelope.json").exists()
    report = json.loads((out / "report.json").read_text())
    assert report["verified"] and report["unresolved"] == 0
    assert report["definitionSha256"] is None
    assert "TestOnto.ttl" in report["output"]["files"]
    assert not any(f.startswith("definition") for f in report["output"]["files"])


def test_ttl_is_fabric_vocabulary_and_matches_the_report(tmp_path, fixtures_dir):
    code, out = run_ttl(tmp_path, fixtures_dir, "--strict")
    report = json.loads((out / "report.json").read_text())
    g = Graph().parse(out / "TestOnto.ttl", format="turtle")
    base = next(g.subjects(RDF.type, OWL.Ontology))
    assert str(base).startswith("https://fabric.microsoft.com/ontology/")
    classes = {str(c).split("#")[-1] for c in g.subjects(RDF.type, OWL.Class)}
    assert classes == {e["name"] for e in report["entities"]} == {"Customer", "Address"}
    assert len(set(g.subjects(RDF.type, OWL.DatatypeProperty))) == report["output"]["properties"]
    assert len(set(g.subjects(RDF.type, OWL.ObjectProperty))) == report["output"]["relationships"]


@pytest.mark.parametrize(
    "flags",
    [
        ["--strict"],
        ["--skip-missing-tables", "--skip-missing-columns"],
        ["--skip-missing-tables", "--skip-missing-columns", "--fuzzy-columns"],
        ["--strict", "--no-property-descriptions", "--no-property-annotations"],
        ["--strict", "--no-fk-properties", "--annotation-exclude", "dataPropertyId,classId"],
        ["--strict", "--entity-naming", "label", "--column-naming", "physical"],
        ["--strict", "--entities", "Customer"],
        ["--strict", "--include-unmapped-columns", "--allow-self-relationships", "--one-relationship-per-pair"],
    ],
)
def test_same_flags_give_the_same_ttl_as_convert(tmp_path, fixtures_dir, flags):
    code_old, old = run_ttl(tmp_path, fixtures_dir, *flags, "--format", "ttl", command="convert", out="old")
    code_new, new = run_ttl(tmp_path, fixtures_dir, *flags, out="new")
    assert code_new == code_old
    assert (new / "TestOnto.ttl").read_bytes() == (old / "TestOnto.ttl").read_bytes()
    for name in ["skipped.csv", "findings.csv"]:
        assert (new / name).read_bytes() == (old / name).read_bytes(), name
    out_old = json.loads((old / "report.json").read_text())
    out_new = json.loads((new / "report.json").read_text())
    assert out_new["options"] == out_old["options"]
    assert out_new["output"]["entities"] == out_old["output"]["entities"]


def test_strict_drops_what_does_not_match_exactly(tmp_path, fixtures_dir):
    code, out = run_ttl(tmp_path, fixtures_dir, "--strict")
    rows = list(csv.DictReader(open(out / "skipped.csv", encoding="utf-8")))
    assert any(r["reason"] == "COLUMN_CASE_MISMATCH" and r["name"] == "Case Prop" for r in rows)
    assert 'rdfs:label "Case Prop"' not in (out / "TestOnto.ttl").read_text(encoding="utf-8")


def test_unresolved_bindings_exit_code(tmp_path, fixtures_dir):
    code, out = run_ttl(tmp_path, fixtures_dir)
    assert code == 2
    assert (out / "TestOnto.ttl").exists()


def test_validation_flags_need_a_catalog(tmp_path, fixtures_dir, capsys):
    for flag in ["--strict", "--skip-missing-tables", "--case-sensitive"]:
        with pytest.raises(SystemExit) as exc:
            main(["convert-ttl", str(fixtures_dir / "mini.ttl"), "--output-dir", str(tmp_path / "x"), flag])
        assert "physical catalog" in str(exc.value)
    assert not (tmp_path / "x" / "TestOnto.ttl").exists()


def test_translates_without_catalog_or_flags(tmp_path, fixtures_dir):
    out = tmp_path / "plain"
    argv = ["convert-ttl", str(fixtures_dir / "mini.ttl"), "-o", str(out), "--name", "Plain", "--log-level", "ERROR"]
    code = main(argv)
    assert code == 0
    assert (out / "Plain.ttl").exists()
    assert not json.loads((out / "report.json").read_text())["verified"]


def test_default_output_dir_is_separate_from_convert(tmp_path, fixtures_dir, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = main(["convert-ttl", str(fixtures_dir / "mini.ttl"), "--name", "TestOnto", "--log-level", "ERROR"])
    assert code == 0
    assert (tmp_path / "build" / "TestOnto-ttl" / "TestOnto.ttl").exists()
    assert not (tmp_path / "build" / "TestOnto").exists()


def test_nothing_to_translate_is_an_error(tmp_path, fixtures_dir):
    code, out = run_ttl(tmp_path, fixtures_dir, "--strict", "--entities", "DoesNotExist")
    assert code == 1
    assert not (out / "TestOnto.ttl").exists()
    report = json.loads((out / "report.json").read_text())
    assert report["output"]["entities"] == 0 and "TestOnto.ttl" not in report["output"]["files"]
    assert (out / "skipped.csv").exists()


def test_existing_definition_is_left_alone_and_no_longer_matches_report(tmp_path, fixtures_dir):
    code, out = run_ttl(tmp_path, fixtures_dir, "--strict", command="convert", out="shared")
    assert code == 0 and (out / "definition" / "model.tmdl").exists()
    before = (out / "definition" / "model.tmdl").read_bytes()
    code, out = run_ttl(tmp_path, fixtures_dir, "--strict", out="shared")
    assert code == 0
    assert (out / "definition" / "model.tmdl").read_bytes() == before
    assert json.loads((out / "report.json").read_text())["definitionSha256"] is None  # deploy refuses it


def test_takes_the_same_flags_as_convert():
    convert, ttl = subcommand_options("convert"), subcommand_options("convert-ttl")
    assert convert - ttl == TMDL_ONLY
    assert ttl - convert == set()
    assert {"--strict", "--output-dir", "--skip-missing-tables", "--skip-missing-columns", "--case-sensitive"} <= ttl


def test_convert_accepts_output_dir_too(tmp_path, fixtures_dir):
    code, out = run_ttl(tmp_path, fixtures_dir, "--strict", command="convert")
    assert code == 0 and (out / "definition" / "model.tmdl").exists() and (out / "TestOnto.ttl").exists()


def test_tmdl_only_flags_are_rejected(tmp_path, fixtures_dir):
    with pytest.raises(SystemExit):
        main(["convert-ttl", str(fixtures_dir / "mini.ttl"), "--format", "ttl", "--output-dir", str(tmp_path)])


def test_says_that_a_ttl_has_no_bindings(tmp_path, fixtures_dir, capsys):
    code, out = run_ttl(tmp_path, fixtures_dir, "--strict", "--log-level", "INFO")
    assert code == 0
    err = capsys.readouterr().err
    assert "NOTE: a TTL carries the schema only" in err and "'convert' and 'deploy'" in err
    assert "valueColumn" not in (out / "TestOnto.ttl").read_text(encoding="utf-8")
