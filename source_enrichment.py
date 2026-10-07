"""Bounded same-story enrichment from explicit primary links, not a new feed."""
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

# Exact reviewed hosts only. No arbitrary URLs, redirects or URL-slug facts.
PRIMARY_HOSTS = {'www.cisa.gov', 'cisa.gov', 'cert.europa.eu', 'cert.ssi.gouv.fr',
    'www.ncsc.gov.uk', 'nvd.nist.gov', 'msrc.microsoft.com', 'www.microsoft.com',
    'support.apple.com', 'security.apple.com', 'security.googleblog.com',
    'blog.google', 'www.mozilla.org', 'www.cve.org', 'www.circl.lu',
    'www.jpcert.or.jp', 'www.csa.gov.sg', 'www.cyber.gov.au', 'www.cert-in.org.in'}

def primary_url(url):
    try:
        p = urlparse(url)
        return p.scheme == 'https' and p.hostname in PRIMARY_HOSTS and p.port in (None, 443) and not p.username and not p.password
    except ValueError:
        return False

def enrich(item):
    result = dict(item)
    evidence = []
    for url in [u for u in item.get('source_links', []) if primary_url(u)][:2]:
        try:
            with requests.get(url, timeout=(3, 7), allow_redirects=False, stream=True,
                              headers={'User-Agent': 'CyberoPlus-News/1.0', 'Accept': 'text/html'}) as response:
                if response.status_code != 200 or 'text/html' not in response.headers.get('Content-Type', '').lower():
                    continue
                data = bytearray()
                for chunk in response.iter_content(16384):
                    data.extend(chunk)
                    if len(data) > 500000:
                        raise ValueError('Primary page exceeds extraction budget')
            soup = BeautifulSoup(bytes(data), 'html.parser')
            title = soup.title.get_text(' ', strip=True) if soup.title else ''
            for node in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'form']):
                node.decompose()
            article = soup.find('article') or soup.find('main')
            if article is None:
                continue
            paragraphs = [re.sub(r'\s+', ' ', p.get_text(' ', strip=True)) for p in article.find_all(['p', 'li'])]
            # Select paragraphs sharing concrete terms with the discovery text;
            # the editorial model must still check they describe the same event.
            terms = set(re.findall(r'[A-Za-z][A-Za-z0-9-]{3,}', item.get('text', '').lower())) - {'this','that','with','from','have','will','been','their','they','more','about','said','into'}
            selected = [p for p in paragraphs if len(p) >= 60 and len(terms & set(re.findall(r'[a-z][a-z0-9-]{3,}', p.lower()))) >= 2]
            excerpt = '\n'.join(selected)[:5000]
            if excerpt:
                evidence.append({'url': url, 'title': title[:250], 'text': excerpt,
                                 'retrieved_at': datetime.now(timezone.utc).isoformat(), 'kind': 'linked_primary_excerpt'})
        except (requests.RequestException, ValueError):
            continue
    result['primary_evidence'] = evidence
    return result
