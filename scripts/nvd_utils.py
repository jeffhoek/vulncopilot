"""Shared NVD parsing and extraction utilities.

Used by both load_nvd.py (KEV-scoped) and load_nvd_full.py (full NVD).
"""

import datetime


def extract_cvss_v31(metrics: dict) -> tuple:
    """Extract CVSS v3.1 score, severity, and vector string."""
    for entry in metrics.get("cvssMetricV31", []):
        data = entry.get("cvssData", {})
        return (
            data.get("baseScore"),
            data.get("baseSeverity"),
            data.get("vectorString"),
        )
    return None, None, None


def extract_cvss_v2(metrics: dict) -> tuple:
    """Extract CVSS v2 score and severity."""
    for entry in metrics.get("cvssMetricV2", []):
        data = entry.get("cvssData", {})
        return data.get("baseScore"), entry.get("baseSeverity")
    return None, None


def extract_cwes(weaknesses: list) -> list[str]:
    """Extract CWE IDs from weaknesses."""
    cwes = []
    for weakness in weaknesses:
        for desc in weakness.get("description", []):
            if desc.get("lang") == "en":
                cwes.append(desc["value"])
    return cwes


def extract_affected_products(configurations: list) -> list[str]:
    """Extract CPE strings from configurations."""
    products = []
    for config in configurations:
        for node in config.get("nodes", []):
            for match in node.get("cpeMatch", []):
                if match.get("vulnerable"):
                    products.append(match.get("criteria", ""))
    return products


def extract_description(descriptions: list) -> str:
    """Extract English description."""
    for desc in descriptions:
        if desc.get("lang") == "en":
            return desc.get("value", "")
    return ""


def extract_reference_urls(references: list) -> list[str]:
    """Extract reference URLs."""
    return [ref.get("url", "") for ref in references[:10]]


def parse_date(date_str: str | None) -> datetime.date | None:
    """Parse ISO-8601 date string to date object."""
    if not date_str:
        return None
    return datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()


def build_content(cve_data: dict) -> str:
    """Build content string for embedding from NVD CVE data."""
    description = extract_description(cve_data.get("descriptions", []))
    metrics = cve_data.get("metrics", {})
    cvss_score, cvss_severity, cvss_vector = extract_cvss_v31(metrics)
    cwes = extract_cwes(cve_data.get("weaknesses", []))
    products = extract_affected_products(cve_data.get("configurations", []))

    parts = [
        f"CVE ID: {cve_data.get('id', '')}",
        f"Description: {description}",
    ]
    if cvss_score is not None:
        parts.append(f"CVSS v3.1 Score: {cvss_score} ({cvss_severity})")
    if cvss_vector:
        parts.append(f"CVSS Vector: {cvss_vector}")
    if cwes:
        parts.append(f"CWEs: {', '.join(cwes)}")
    if products:
        parts.append(f"Affected Products: {', '.join(products[:5])}")

    return "\n".join(parts)
