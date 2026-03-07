import json
from pathlib import Path

import pytest
import yaml

import main


def test_parse_rule_source_supports_plain_lines_and_warns_for_unsupported_items(caplog):
    text = "\n".join(
        [
            "# comment",
            "DOMAIN,a.com",
            ".example.com",
            "IP-CIDR,1.1.1.0/24,no-resolve",
            "DST-PORT,443",
            "GEOIP,CN",
            "AND,(DOMAIN,a.com),(DST-PORT,443)",
        ]
    )

    field_values = main.parse_rule_source(text, source_name="sample.list")

    assert field_values["domain"] == {"a.com"}
    assert field_values["domain_suffix"] == {"example.com"}
    assert field_values["ip_cidr"] == {"1.1.1.0/24"}
    assert field_values["port"] == {"443"}
    assert "geoip" not in field_values

    warning_messages = [record.message for record in caplog.records]
    assert any("deprecated rule type" in message for message in warning_messages)
    assert any("unsupported logical rule" in message for message in warning_messages)


def test_parse_rule_source_supports_yaml_payload_and_plain_item_inference():
    payload = yaml.safe_dump(
        {
            "payload": [
                "DOMAIN,alpha.example",
                "+beta.example",
                "192.0.2.0/24",
            ]
        },
        sort_keys=False,
    )

    field_values = main.parse_rule_source(payload, source_name="payload.yaml")

    assert field_values["domain"] == {"alpha.example"}
    assert field_values["domain_suffix"] == {"beta.example"}
    assert field_values["ip_cidr"] == {"192.0.2.0/24"}


@pytest.mark.parametrize(
    ("field_values", "expected_names"),
    [
        ({"domain": {"a.com"}}, {"Sample.json"}),
        ({"ip_cidr": {"192.0.2.0/24"}}, {"Sample.json"}),
        ({"port": {"443"}, "process_name": {"curl"}}, {"Sample.json"}),
        (
            {"domain": {"a.com"}, "port": {"443"}},
            {"Sample.json", "DNS_Sample_domain.json"},
        ),
        (
            {"ip_cidr": {"192.0.2.0/24"}, "process_name": {"curl"}},
            {"Sample.json", "DNS_Sample_ipcidr.json"},
        ),
        (
            {"domain": {"a.com"}, "ip_cidr": {"192.0.2.0/24"}},
            {"Sample.json", "DNS_Sample_domain.json", "DNS_Sample_ipcidr.json"},
        ),
        (
            {"domain": {"a.com"}, "ip_cidr": {"192.0.2.0/24"}, "port": {"443"}},
            {"Sample.json", "DNS_Sample_domain.json", "DNS_Sample_ipcidr.json"},
        ),
    ],
)
def test_build_documents_splits_outputs_by_dns_use_case(field_values, expected_names):
    documents = main.build_documents("Sample", field_values)

    assert set(documents) == expected_names
    for document in documents.values():
        assert document["version"] == 4

    if "DNS_Sample_domain.json" in documents:
        dns_domain_fields = {
            next(iter(rule.keys()))
            for rule in documents["DNS_Sample_domain.json"]["rules"]
        }
        assert dns_domain_fields <= main.DOMAIN_FIELDS

    if "DNS_Sample_ipcidr.json" in documents:
        dns_ip_fields = {
            next(iter(rule.keys()))
            for rule in documents["DNS_Sample_ipcidr.json"]["rules"]
        }
        assert dns_ip_fields == {"ip_cidr"}


def test_build_documents_preserves_non_dns_fields_only_in_generic_output():
    documents = main.build_documents(
        "Sample",
        {
            "domain": {"a.com"},
            "ip_cidr": {"192.0.2.0/24"},
            "process_name": {"curl"},
            "source_port": {"12345"},
        },
    )

    generic_fields = {next(iter(rule.keys())) for rule in documents["Sample.json"]["rules"]}
    dns_domain_fields = {
        next(iter(rule.keys()))
        for rule in documents["DNS_Sample_domain.json"]["rules"]
    }
    dns_ip_fields = {
        next(iter(rule.keys()))
        for rule in documents["DNS_Sample_ipcidr.json"]["rules"]
    }

    assert {"process_name", "source_port"} <= generic_fields
    assert "process_name" not in dns_domain_fields
    assert "source_port" not in dns_domain_fields
    assert dns_ip_fields == {"ip_cidr"}


def test_run_generates_outputs_and_manifest(tmp_path, monkeypatch):
    links_path = tmp_path / "links.txt"
    links_path.write_text("https://example.com/Alpha.list\n", encoding="utf-8")

    output_dir = tmp_path / "rule"
    compile_calls = []

    def fake_fetch_text(url: str) -> str:
        assert url == "https://example.com/Alpha.list"
        return "\n".join(
            [
                "DOMAIN,alpha.example",
                "IP-CIDR,192.0.2.0/24",
                "DST-PORT,443",
            ]
        )

    def fake_compile_rule_set(json_path: Path, srs_path: Path, sing_box_bin: str) -> None:
        compile_calls.append((json_path.name, srs_path.name, sing_box_bin))
        srs_path.write_text("compiled", encoding="utf-8")

    monkeypatch.setattr(main, "fetch_text", fake_fetch_text)
    monkeypatch.setattr(main, "compile_rule_set", fake_compile_rule_set)

    generated = main.run(links_path=links_path, output_dir=output_dir, sing_box_bin="sing-box")

    assert {path.name for path in generated} == {
        "Alpha.json",
        "Alpha.srs",
        "DNS_Alpha_domain.json",
        "DNS_Alpha_domain.srs",
        "DNS_Alpha_ipcidr.json",
        "DNS_Alpha_ipcidr.srs",
        ".generated-manifest.json",
    }
    assert compile_calls == [
        ("Alpha.json", "Alpha.srs", "sing-box"),
        ("DNS_Alpha_domain.json", "DNS_Alpha_domain.srs", "sing-box"),
        ("DNS_Alpha_ipcidr.json", "DNS_Alpha_ipcidr.srs", "sing-box"),
    ]

    alpha_doc = json.loads((output_dir / "Alpha.json").read_text(encoding="utf-8"))
    dns_domain_doc = json.loads((output_dir / "DNS_Alpha_domain.json").read_text(encoding="utf-8"))
    dns_ip_doc = json.loads((output_dir / "DNS_Alpha_ipcidr.json").read_text(encoding="utf-8"))
    manifest = json.loads((output_dir / ".generated-manifest.json").read_text(encoding="utf-8"))

    assert alpha_doc["version"] == 4
    assert dns_domain_doc["rules"] == [{"domain": ["alpha.example"]}]
    assert dns_ip_doc["rules"] == [{"ip_cidr": ["192.0.2.0/24"]}]
    assert sorted(manifest["generated_files"]) == [
        "Alpha.json",
        "Alpha.srs",
        "DNS_Alpha_domain.json",
        "DNS_Alpha_domain.srs",
        "DNS_Alpha_ipcidr.json",
        "DNS_Alpha_ipcidr.srs",
    ]


def test_run_cleans_up_stale_generated_files(tmp_path, monkeypatch):
    links_path = tmp_path / "links.txt"
    links_path.write_text("https://example.com/Alpha.list\n", encoding="utf-8")
    output_dir = tmp_path / "rule"

    payloads = iter(
        [
            "\n".join(
                [
                    "IP-CIDR,192.0.2.0/24",
                    "DST-PORT,443",
                ]
            ),
            "IP-CIDR,192.0.2.0/24",
        ]
    )

    def fake_fetch_text(_url: str) -> str:
        return next(payloads)

    def fake_compile_rule_set(json_path: Path, srs_path: Path, _sing_box_bin: str) -> None:
        srs_path.write_text(f"compiled:{json_path.name}", encoding="utf-8")

    monkeypatch.setattr(main, "fetch_text", fake_fetch_text)
    monkeypatch.setattr(main, "compile_rule_set", fake_compile_rule_set)

    main.run(links_path=links_path, output_dir=output_dir, sing_box_bin="sing-box")
    main.run(links_path=links_path, output_dir=output_dir, sing_box_bin="sing-box")

    assert (output_dir / "Alpha.json").exists()
    assert (output_dir / "Alpha.srs").exists()
    assert not (output_dir / "DNS_Alpha_domain.json").exists()
    assert not (output_dir / "DNS_Alpha_domain.srs").exists()
    assert not (output_dir / "DNS_Alpha_ipcidr.json").exists()
    assert not (output_dir / "DNS_Alpha_ipcidr.srs").exists()

    manifest = json.loads((output_dir / ".generated-manifest.json").read_text(encoding="utf-8"))
    assert manifest["generated_files"] == ["Alpha.json", "Alpha.srs"]


def test_run_with_ip_cidr_only_generates_generic_output_only(tmp_path, monkeypatch):
    links_path = tmp_path / "links.txt"
    links_path.write_text("https://example.com/Alpha.list\n", encoding="utf-8")

    output_dir = tmp_path / "rule"
    compile_calls = []

    def fake_fetch_text(_url: str) -> str:
        return "IP-CIDR,192.0.2.0/24"

    def fake_compile_rule_set(json_path: Path, srs_path: Path, sing_box_bin: str) -> None:
        compile_calls.append((json_path.name, srs_path.name, sing_box_bin))
        srs_path.write_text("compiled", encoding="utf-8")

    monkeypatch.setattr(main, "fetch_text", fake_fetch_text)
    monkeypatch.setattr(main, "compile_rule_set", fake_compile_rule_set)

    generated = main.run(links_path=links_path, output_dir=output_dir, sing_box_bin="sing-box")

    assert {path.name for path in generated} == {
        "Alpha.json",
        "Alpha.srs",
        ".generated-manifest.json",
    }
    assert compile_calls == [("Alpha.json", "Alpha.srs", "sing-box")]
    assert not (output_dir / "DNS_Alpha_ipcidr.json").exists()
    assert not (output_dir / "DNS_Alpha_ipcidr.srs").exists()


def test_run_bootstraps_cleanup_when_manifest_is_missing(tmp_path, monkeypatch):
    links_path = tmp_path / "links.txt"
    links_path.write_text("https://example.com/Alpha.list\n", encoding="utf-8")
    output_dir = tmp_path / "rule"
    output_dir.mkdir()

    (output_dir / "Advertising.json").write_text("legacy", encoding="utf-8")
    (output_dir / "Advertising.srs").write_text("legacy", encoding="utf-8")
    (output_dir / "placehold.txt").write_text("", encoding="utf-8")

    def fake_fetch_text(_url: str) -> str:
        return "DOMAIN,alpha.example"

    def fake_compile_rule_set(json_path: Path, srs_path: Path, _sing_box_bin: str) -> None:
        srs_path.write_text(f"compiled:{json_path.name}", encoding="utf-8")

    monkeypatch.setattr(main, "fetch_text", fake_fetch_text)
    monkeypatch.setattr(main, "compile_rule_set", fake_compile_rule_set)

    main.run(links_path=links_path, output_dir=output_dir, sing_box_bin="sing-box")

    assert not (output_dir / "Advertising.json").exists()
    assert not (output_dir / "Advertising.srs").exists()
    assert (output_dir / "Alpha.json").exists()
    assert (output_dir / "Alpha.srs").exists()
    assert (output_dir / "placehold.txt").exists()


def test_run_cleans_orphaned_generated_files_even_if_manifest_exists(tmp_path, monkeypatch):
    links_path = tmp_path / "links.txt"
    links_path.write_text("https://example.com/Alpha.list\n", encoding="utf-8")
    output_dir = tmp_path / "rule"
    output_dir.mkdir()

    (output_dir / "Advertising.json").write_text("legacy", encoding="utf-8")
    (output_dir / "Advertising.srs").write_text("legacy", encoding="utf-8")
    (output_dir / ".generated-manifest.json").write_text(
        json.dumps({"generated_files": ["Alpha.json", "Alpha.srs"]}),
        encoding="utf-8",
    )

    def fake_fetch_text(_url: str) -> str:
        return "DOMAIN,alpha.example"

    def fake_compile_rule_set(json_path: Path, srs_path: Path, _sing_box_bin: str) -> None:
        srs_path.write_text(f"compiled:{json_path.name}", encoding="utf-8")

    monkeypatch.setattr(main, "fetch_text", fake_fetch_text)
    monkeypatch.setattr(main, "compile_rule_set", fake_compile_rule_set)

    main.run(links_path=links_path, output_dir=output_dir, sing_box_bin="sing-box")

    assert not (output_dir / "Advertising.json").exists()
    assert not (output_dir / "Advertising.srs").exists()
