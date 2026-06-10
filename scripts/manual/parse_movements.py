#!/usr/bin/env python3
"""Debug movement parsing."""

import re

# Read the detail HTML
with open("screenshots/detalle_civil.html", "r") as f:
    html = f.read()

# Find the history table - look for table with Fec. Trámite header
table_match = re.search(
    r'<table[^>]*class="[^"]*table-bordered[^"]*"[^>]*>.*?<tbody>(.*?)</tbody>',
    html,
    re.DOTALL | re.IGNORECASE
)

if not table_match:
    print("Table not found!")
    exit(1)

tbody = table_match.group(1)
print(f"Found tbody: {len(tbody)} chars")
print()

# Extract each row
row_pattern = r'<tr[^>]*>(.*?)</tr>'
rows = list(re.finditer(row_pattern, tbody, re.DOTALL | re.IGNORECASE))
print(f"Found {len(rows)} rows")
print()

for i, row_match in enumerate(rows[:2]):
    row = row_match.group(1)
    print(f"=== ROW {i} ===")
    
    # Split by </td>
    raw_cells = re.split(r'</td>', row, flags=re.IGNORECASE)
    print(f"Split into {len(raw_cells)} parts")
    
    for j, cell in enumerate(raw_cells[:9]):
        # Remove opening td tag
        content = re.sub(r'^.*?<td[^>]*>', '', cell, flags=re.IGNORECASE | re.DOTALL)
        # Clean
        clean = re.sub(r'<[^>]+>', '', content)
        clean = re.sub(r'\s+', ' ', clean).strip()
        print(f"  Cell {j}: {clean[:60]}...")
    
    print()
