import re, email, urllib.parse as up
from email import policy

TAG_RE = re.compile(
    r"\[(?P<job>[A-Z]\d+)\s+(?P<locale>[a-z]{2}_[A-Z]{2})\s+"
    r"(?P<asset>\S+)\s+Proof\s+(?P<proof>\d+)\s+(?P<fmt>\S+)\]\s*(?P<subject>.*)"
)

def parse_eml(path):
    msg = email.message_from_binary_file(open(path, "rb"), policy=policy.default)
    raw_subject = str(msg["Subject"])
    m = TAG_RE.match(raw_subject)
    tag = m.groupdict() if m else None          # None -> flag "no tag ID"
    html = msg.get_body(preferencelist=("html",))
    text = msg.get_body(preferencelist=("plain",))
    return {
        "path": path,
        "tag": tag,
        "subject": tag["subject"] if tag else raw_subject,
        "html": html.get_content() if html else None,
        "text": text.get_content() if text else None,
    }

def unwrap_safelinks(url):
    p = up.urlparse(url)
    if p.netloc.endswith("safelinks.protection.outlook.com"):
        return up.parse_qs(p.query).get("url", [url])[0]
    return url

def clean_label(s):
    return re.sub(r"\s+", " ", s).strip().lower()   # fixes "Read\n online"
