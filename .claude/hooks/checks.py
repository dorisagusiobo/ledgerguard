"""Claude Code Write/Edit hooks; no third-party dependencies."""
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CARD = re.compile(r"(?<![A-Za-z0-9_])(?:\d[ -]?){12,18}\d(?![A-Za-z0-9_])")
ACCOUNT = re.compile(
    r"\b(?:bank[ _-]*account|account[ _-]*(?:number|no)|routing[ _-]*(?:number|no)?|"
    r"ssn|social[ _-]*security|card[ _-]*(?:number|no))\b"
    r"[^\n\d]{0,30}(?:\d[ -]?){4,}", re.I
)
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b")


def luhn(value):
    digits = [int(c) for c in value if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def iban_checksum(value):
    """Check mod-97 on an IBAN-shaped candidate, not account existence."""
    compact = value.replace(' ', '').upper()
    if not re.fullmatch(r'[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}', compact):
        return False
    rearranged = compact[4:] + compact[:4]
    numeric = ''.join(str(ord(c) - ord('A') + 10) if c.isalpha() else c
                      for c in rearranged)
    return int(numeric) % 97 == 1


def suspected_financial_data(text):
    return bool(SSN.search(text) or ACCOUNT.search(text) or any(iban_checksum(m.group()) for m in IBAN.finditer(text))
                or any(luhn(m.group()) for m in CARD.finditer(text)))


def proposed_text(tool, args):
    if tool == 'Write':
        return args['content']
    path = Path(args['file_path'])
    if not path.is_absolute():
        path = ROOT / path
    # Do not read credential files. The caller checks the path first.
    old = path.read_text()
    before = args['old_string']
    after = args['new_string']
    if not before:
        return after
    if before not in old:
        raise ValueError('Edit target not found')
    return old.replace(before, after, -1 if args.get('replace_all') else 1)


def guard(event):
    args = event['tool_input']
    path = Path(args['file_path'])
    if any(part == '.aws' for part in path.parts) or path.name == '.env' or path.name.startswith('.env.'):
        print('Blocked: credential/environment files must not be edited by this tool.', file=sys.stderr)
        return 2
    text = proposed_text(event['tool_name'], args)
    if suspected_financial_data(text):
        print('Blocked: suspected financial identifier. Use nonnumeric placeholders '
              'such as TEST-CARD-001 or TEST-BANK-001, even for synthetic fixtures. '
              'Do not echo the matched value.', file=sys.stderr)
        return 2
    return 0


def test_feedback(result):
    if result.returncode == 5:
        return 'no tests (pytest exit 5); neither a failure nor a pass', 0
    if result.returncode == 0:
        return 'Offline tests passed.\n' + result.stdout, 0
    return 'Offline tests failed; fix before continuing.\n' + result.stdout + result.stderr, 2


def after_edit(event):
    if Path(event['tool_input']['file_path']).suffix != '.py':
        return 0
    python = ROOT / '.venv/bin/python'
    if not python.exists():
        print('Tests could not run: run uv sync to create .venv first.', file=sys.stderr)
        return 2
    try:
        result = subprocess.run([str(python), '-m', 'pytest', '-q'], cwd=ROOT,
                                capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        print('Offline tests exceeded 45 seconds; run them manually and investigate.', file=sys.stderr)
        return 2
    message, status = test_feedback(result)
    if status:
        print(message, file=sys.stderr)
    else:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse',
                                               'additionalContext': message}}))
    return status


def main():
    try:
        event = json.load(sys.stdin)
        if event.get('tool_name') not in ('Write', 'Edit'):
            return 0
        return guard(event) if sys.argv[1] == 'guard' else after_edit(event)
    except (ValueError, KeyError, OSError, IndexError):
        print('Hook could not validate the operation; inspect configuration before retrying.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
