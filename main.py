from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import urlparse

import requests
import yaml

LOGGER = logging.getLogger(__name__)

MANIFEST_NAME = ".generated-manifest.json"
DOMAIN_FIELDS = frozenset({"domain", "domain_suffix", "domain_keyword", "domain_regex"})
FIELD_ORDER = [
    "domain",
    "domain_suffix",
    "domain_keyword",
    "domain_regex",
    "ip_cidr",
    "source_ip_cidr",
    "port",
    "port_range",
    "source_port",
    "source_port_range",
    "process_name",
    "process_path",
    "process_path_regex",
    "package_name",
    "network",
]
PATTERN_MAP = {
    "DOMAIN": "domain",
    "HOST": "domain",
    "DOMAIN-SUFFIX": "domain_suffix",
    "HOST-SUFFIX": "domain_suffix",
    "DOMAIN-KEYWORD": "domain_keyword",
    "HOST-KEYWORD": "domain_keyword",
    "URL-REGEX": "domain_regex",
    "DOMAIN-REGEX": "domain_regex",
    "IP-CIDR": "ip_cidr",
    "IP-CIDR6": "ip_cidr",
    "IP6-CIDR": "ip_cidr",
    "SRC-IP-CIDR": "source_ip_cidr",
    "PROCESS-NAME": "process_name",
    "PROCESS-PATH": "process_path",
    "PROCESS-PATH-REGEX": "process_path_regex",
    "PACKAGE-NAME": "package_name",
    "NETWORK": "network",
}
DEPRECATED_PATTERNS = {"GEOIP", "SOURCE-GEOIP", "SRC-GEOIP", "GEOSITE"}
LOGICAL_PATTERNS = {"AND", "OR", "NOT"}


def read_links(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def fetch_text(url: str) -> str:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.text


def parse_rule_source(text: str, source_name: str = "") -> dict[str, set[str]]:
    payload_items = extract_payload_items(text)
    items = payload_items if payload_items is not None else text.splitlines()
    field_values: dict[str, set[str]] = defaultdict(set)

    for raw_item in items:
        parsed = parse_rule_item(str(raw_item), source_name=source_name)
        if parsed is None:
            continue
        field_name, value = parsed
        field_values[field_name].add(value)

    return dict(field_values)


def extract_payload_items(text: str) -> list[str] | None:
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if isinstance(loaded, dict) and isinstance(loaded.get("payload"), list):
        return [str(item) for item in loaded["payload"]]
    return None


def parse_rule_item(raw_item: str, source_name: str = "") -> tuple[str, str] | None:
    item = raw_item.strip().strip("'").strip('"')
    if not item or item.startswith("#"):
        return None

    if "," in item:
        pattern_token, remainder = item.split(",", 1)
        pattern_key = pattern_token.strip().upper()
        if pattern_key in LOGICAL_PATTERNS:
            LOGGER.warning("Skipping unsupported logical rule in %s: %s", source_name or "<input>", item)
            return None
        if pattern_key in DEPRECATED_PATTERNS:
            LOGGER.warning("Skipping deprecated rule type %s in %s", pattern_key, source_name or "<input>")
            return None

        value = remainder.split(",", 1)[0].strip()
        field_name = normalize_pattern(pattern_key, value)
        if field_name is None:
            LOGGER.warning("Skipping unsupported rule type %s in %s", pattern_key, source_name or "<input>")
            return None
        normalized_value = normalize_value(field_name, value)
        if normalized_value is None:
            LOGGER.warning("Skipping invalid value for %s in %s: %s", field_name, source_name or "<input>", item)
            return None
        return field_name, normalized_value

    inferred_field, inferred_value = infer_rule_item(item)
    normalized_value = normalize_value(inferred_field, inferred_value)
    if normalized_value is None:
        LOGGER.warning("Skipping invalid inferred value in %s: %s", source_name or "<input>", item)
        return None
    return inferred_field, normalized_value


def normalize_pattern(pattern_key: str, value: str) -> str | None:
    if pattern_key == "DST-PORT":
        return "port_range" if is_port_range(value) else "port"
    if pattern_key == "SRC-PORT":
        return "source_port_range" if is_port_range(value) else "source_port"
    return PATTERN_MAP.get(pattern_key)


def is_port_range(value: str) -> bool:
    return any(separator in value for separator in ("-", ":"))


def infer_rule_item(item: str) -> tuple[str, str]:
    value = item.strip()
    if value.startswith(("+", ".")):
        return "domain_suffix", value.lstrip("+.")
    if is_ip_or_cidr(value):
        return "ip_cidr", value
    return "domain", value


def is_ip_or_cidr(value: str) -> bool:
    try:
        ipaddress.ip_network(value, strict=False)
        return True
    except ValueError:
        return False


def normalize_value(field_name: str, value: str) -> str | None:
    normalized = value.strip()
    if not normalized:
        return None

    if field_name in DOMAIN_FIELDS:
        return normalized.lower()
    if field_name in {"ip_cidr", "source_ip_cidr"}:
        if not is_ip_or_cidr(normalized):
            return None
    return normalized


def build_documents(stem: str, field_values: Mapping[str, set[str]]) -> dict[str, dict[str, object]]:
    clean_values = {field: values for field, values in field_values.items() if values}
    documents: dict[str, dict[str, object]] = {}

    generic_rules = build_rule_list(clean_values)
    if generic_rules:
        documents[f"{stem}.json"] = {"version": 4, "rules": generic_rules}

    domain_values = {field: clean_values[field] for field in DOMAIN_FIELDS if field in clean_values}
    ipcidr_values = {"ip_cidr": clean_values["ip_cidr"]} if "ip_cidr" in clean_values else {}
    has_other_values = any(field not in DOMAIN_FIELDS and field != "ip_cidr" for field in clean_values)

    if domain_values and (ipcidr_values or has_other_values):
        documents[f"DNS_{stem}_domain.json"] = {"version": 4, "rules": build_rule_list(domain_values)}
    if ipcidr_values:
        documents[f"DNS_{stem}_ipcidr.json"] = {"version": 4, "rules": build_rule_list(ipcidr_values)}

    return documents


def build_rule_list(field_values: Mapping[str, set[str]]) -> list[dict[str, list[str]]]:
    rules: list[dict[str, list[str]]] = []
    for field_name in FIELD_ORDER:
        values = field_values.get(field_name)
        if values:
            rules.append({field_name: sorted(values)})
    return rules


def canonical_stem(url: str) -> str:
    stem = Path(urlparse(url).path).stem
    if not stem:
        raise ValueError(f"unable to derive output stem from url: {url}")
    return stem


def write_document(path: Path, document: Mapping[str, object]) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def compile_rule_set(json_path: Path, srs_path: Path, sing_box_bin: str) -> None:
    try:
        subprocess.run(
            [sing_box_bin, "rule-set", "compile", "--output", str(srs_path), str(json_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"failed to compile {json_path.name} with {sing_box_bin}: {exc.stderr.strip() or exc.stdout.strip()}"
        ) from exc


def load_manifest(path: Path) -> set[str]:
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data.get("generated_files", []))


def write_manifest(path: Path, generated_files: Iterable[str]) -> None:
    document = {"generated_files": sorted(generated_files)}
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cleanup_stale_files(output_dir: Path, stale_files: Iterable[str]) -> None:
    for relative_name in stale_files:
        stale_path = output_dir / relative_name
        if stale_path.exists():
            stale_path.unlink()


def discover_legacy_generated_files(output_dir: Path) -> set[str]:
    legacy_files: set[str] = set()
    for path in output_dir.iterdir():
        if not path.is_file():
            continue
        if path.name == MANIFEST_NAME:
            continue
        if path.suffix not in {".json", ".srs"}:
            continue
        legacy_files.add(path.name)
    return legacy_files


def run(links_path: Path, output_dir: Path, sing_box_bin: str = "sing-box") -> list[Path]:
    links = read_links(links_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / MANIFEST_NAME
    previous_files = load_manifest(manifest_path)
    discovered_files = discover_legacy_generated_files(output_dir)
    if not previous_files and not manifest_path.exists():
        previous_files = discovered_files

    current_files: set[str] = set()
    generated_paths: list[Path] = []

    for url in links:
        stem = canonical_stem(url)
        source_text = fetch_text(url)
        field_values = parse_rule_source(source_text, source_name=url)
        documents = build_documents(stem, field_values)
        if not documents:
            LOGGER.warning("Skipping %s because no supported rules remain after filtering", url)
            continue

        for file_name, document in documents.items():
            json_path = output_dir / file_name
            write_document(json_path, document)
            generated_paths.append(json_path)
            current_files.add(file_name)

            srs_path = json_path.with_suffix(".srs")
            compile_rule_set(json_path, srs_path, sing_box_bin)
            generated_paths.append(srs_path)
            current_files.add(srs_path.name)

    cleanup_stale_files(output_dir, (previous_files | discovered_files) - current_files)
    write_manifest(manifest_path, current_files)
    generated_paths.append(manifest_path)
    return generated_paths


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert remote Clash-style rules into sing-box rule-sets.")
    parser.add_argument("--links", default="links.txt", type=Path, help="Path to the links.txt input file.")
    parser.add_argument("--output-dir", default="rule", type=Path, help="Directory for generated JSON and SRS files.")
    parser.add_argument("--sing-box-bin", default="sing-box", help="Path to the sing-box binary.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args(argv)
    generated_paths = run(args.links, args.output_dir, sing_box_bin=args.sing_box_bin)
    LOGGER.info("Generated %d files", len(generated_paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
