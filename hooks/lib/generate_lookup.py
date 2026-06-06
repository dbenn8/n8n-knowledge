#!/usr/bin/env python3
"""Generate node_lookup_data.json from an n8n nodes.db SQLite database."""
import json
import re
import sqlite3
import sys


def generate(db_path, output_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    non_triggers = []
    triggers = []
    for row in conn.execute('SELECT node_type, display_name, is_trigger FROM nodes'):
        (triggers if row['is_trigger'] else non_triggers).append(dict(row))

    # Sort: prefer nodes-base over community packages
    non_triggers.sort(key=lambda r: (0 if r['node_type'].startswith('nodes-base.') else 1))
    triggers.sort(key=lambda r: (0 if r['node_type'].startswith('nodes-base.') else 1))

    entries = {}

    def add_entry(key, nt, overwrite=True):
        if not overwrite and key in entries:
            return
        if key in entries and entries[key].startswith('nodes-base.') and not nt.startswith('nodes-base.'):
            return
        entries[key] = nt

    # Pass 1: non-trigger nodes (priority)
    for row in non_triggers:
        nt = row['node_type']
        dn = row['display_name'].lower().strip()
        raw_suffix = nt.split('.')[-1]
        suffix = raw_suffix.lower()
        add_entry(dn, nt)
        add_entry(suffix, nt)
        split = re.sub(r'([a-z])([A-Z])', r'\1 \2', raw_suffix).lower()
        if split != suffix:
            add_entry(split, nt)

    # Pass 2: trigger nodes (fill gaps only)
    for row in triggers:
        nt = row['node_type']
        dn = row['display_name'].lower().strip()
        raw_suffix = nt.split('.')[-1]
        suffix = raw_suffix.lower()
        add_entry(dn, nt, overwrite=False)
        add_entry(suffix, nt, overwrite=False)
        split = re.sub(r'([a-z])([A-Z])', r'\1 \2', raw_suffix).lower()
        if split != suffix:
            add_entry(split, nt, overwrite=False)
        base = re.sub(r'trigger$', '', suffix)
        if base and base != suffix:
            add_entry(base, nt, overwrite=False)

    conn.close()

    with open(output_path, 'w') as f:
        json.dump(entries, f, indent=0, sort_keys=True)

    print(f"Wrote {len(entries)} entries to {output_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <nodes.db path> <output.json path>", file=sys.stderr)
        sys.exit(1)
    generate(sys.argv[1], sys.argv[2])
