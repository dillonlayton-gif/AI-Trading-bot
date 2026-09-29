"""Scan tracked project text without printing any suspected secret values."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'private_key': re.compile(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----'),
    'provider_token': re.compile(r'\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]{20,})\b'),
    'aws_access_key': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'credential_assignment': re.compile(r'''(?ix)\b(?:api[_-]?key|api[_-]?secret|secret[_-]?key|private[_-]?key|access[_-]?token|client[_-]?secret|password)\b\s*["']?\s*[:=]\s*["']([^"'\n]{10,})["']'''),
}


def main():
    files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    findings, count = [], 0
    for relative in filter(None, files):
        path = ROOT / relative
        if not path.is_file():
            continue
        content = path.read_text(encoding='utf-8')
        count += 1
        for category, pattern in PATTERNS.items():
            for match in pattern.finditer(content):
                if category == 'credential_assignment' and any(word in match.group(1).lower() for word in
                        ('example', 'placeholder', 'your_', 'changeme', 'redacted')):
                    continue
                line = content.count('\n', 0, match.start()) + 1
                findings.append(f'{relative}:{line} ({category}; value omitted)')
    print(f'Scanned {count} tracked text files; {len(findings)} credential/private-key pattern matches.')
    for finding in findings:
        print(finding)
    return bool(findings)


if __name__ == '__main__':
    raise SystemExit(main())
