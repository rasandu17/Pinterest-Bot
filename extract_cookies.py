"""
extract_cookies.py

Reads a raw, giant `cookies.txt` (Netscape format) from your browser export.
It extracts ONLY Instagram and Pinterest cookies and saves them into respective files.
It also creates base64 encoded versions which are useful for hosting on platforms like Koyeb/Heroku.
"""

import base64
import os

INPUT_FILE = "cookies.txt"
IG_OUTPUT = "instagram_cookies.txt"
PIN_OUTPUT = "pinterest_cookies.txt"

def extract_cookies(domain_match: str, output_file: str):
    if not os.path.exists(INPUT_FILE):
        print(f"ERROR: {INPUT_FILE} not found. Please export your browser cookies as cookies.txt and place it next to this script.")
        return

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    target_lines = []
    for line in lines:
        stripped = line.strip()
        # Keep comment/header lines exactly as they are needed for Netscape parsing
        if stripped.startswith("#") or not stripped:
            if not target_lines or not target_lines[-1].startswith("#"):
                target_lines.append(line)
            continue
            
        # Match target domains
        lower = stripped.lower()
        if domain_match in lower:
            target_lines.append(line)

    final_text = "".join(target_lines)
    b64_text = base64.b64encode(final_text.encode("utf-8")).decode("utf-8")
    b64_output = output_file.replace(".txt", "_b64.txt")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(final_text)

    with open(b64_output, "w", encoding="utf-8") as f:
        f.write(b64_text)
        
    line_count = len([l for l in target_lines if l.strip() and not l.strip().startswith('#')])
    print(f"[{domain_match.upper()} Extract]")
    print(f" Saved  : {output_file} ({line_count} cookies)")
    print(f" Base64 : {b64_output} ({len(b64_text):,} chars)")
    print("-" * 40)

if __name__ == "__main__":
    print(f"Reading from {INPUT_FILE}...\n")
    extract_cookies("instagram", IG_OUTPUT)
    extract_cookies("pinterest", PIN_OUTPUT)
    print("Done! You can use these generated files for PinBot (.env or locally).")
