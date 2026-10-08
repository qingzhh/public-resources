#!/usr/bin/env python3
"""Generate Surge files from the owned domain list; no network dependencies."""
import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]

def outputs():
    data = json.loads((ROOT / "network/pt-dns/domains.json").read_text())
    if set(data) != {"resolver", "entries"}:
        raise ValueError("Expected resolver and entries")
    resolver = data["resolver"]
    url = urlparse(resolver)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment or any(c.isspace() for c in resolver) or any(c in resolver for c in ",#"):
        raise ValueError("Resolver must be a single HTTPS DoH endpoint")
    if not isinstance(data["entries"], list) or not data["entries"]:
        raise ValueError("entries must be a nonempty list")
    seen = set()
    entries = []
    for entry in data["entries"]:
        if set(entry) != {"domain", "scope"}:
            raise ValueError("Expected domain and scope")
        domain, scope = entry["domain"], entry["scope"]
        if not isinstance(domain, str) or len(domain) > 253 or "." not in domain or domain != domain.lower():
            raise ValueError(f"Invalid domain: {domain!r}")
        if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in domain.split(".")):
            raise ValueError(f"Invalid domain: {domain!r}")
        if scope not in {"exact", "suffix"} or domain in seen:
            raise ValueError(f"Duplicate domain or invalid scope: {entry!r}")
        seen.add(domain)
        entries.append((domain, scope))
    hosts, rules = [], []
    for domain, scope in sorted(entries):
        hosts.append(f"{domain} = server:{resolver}")
        if scope == "suffix":
            hosts.append(f"*.{domain} = server:{resolver}")
        rules.append(f"{'DOMAIN-SUFFIX' if scope == 'suffix' else 'DOMAIN'},{domain}")
    return {
        "network/pt-dns/pt-dns.sgmodule": "#!name=PT DNS - Cloudflare\n#!desc=PT domain DNS overrides only; routing remains in the main profile.\n\n[Host]\n" + "\n".join(hosts) + "\n",
        "network/pt-dns/pt.list": "# Generated from domains.json; no policy names in this rule set.\n" + "\n".join(rules) + "\n",
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Verify checked-in outputs")
    args = parser.parse_args()
    stale = []
    for name, content in outputs().items():
        path = ROOT / name
        if args.check:
            if not path.exists() or path.read_text() != content:
                stale.append(name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    if stale:
        raise SystemExit("Regenerate files: " + ", ".join(stale))
    print("PT DNS outputs validated" if args.check else "PT DNS outputs generated")

if __name__ == "__main__":
    main()
