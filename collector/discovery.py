"""Extract relationships from named sections, never from all listing links."""
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse


class Sections(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.results = {'same_developer': [], 'similar_games': []}
        self.found = set()
        self.links = {}
        self.heading = False

    def handle_starttag(self, tag, attrs):
        if tag == 'section':
            self.stack.append({'title': '', 'packages': [], 'links': []})
        if tag == 'h2':
            self.heading = True
        if tag == 'a' and self.stack:
            href = dict(attrs).get('href', '')
            parsed = urlparse(href)
            if parsed.hostname in (None, 'play.google.com') and parsed.path in ('/store/apps/dev', '/store/apps/developer', '/store/apps/collection/cluster'):
                self.stack[-1]['links'].append(href)
            if parsed.path == '/store/apps/details' and parsed.hostname in (None, 'play.google.com'):
                package = parse_qs(parsed.query).get('id', [''])[0]
                if package:
                    self.stack[-1]['packages'].append(package)

    def handle_data(self, data):
        if self.heading and self.stack:
            self.stack[-1]['title'] += data

    def handle_endtag(self, tag):
        if tag == 'h2':
            self.heading = False
        if tag == 'section' and self.stack:
            section = self.stack.pop()
            title = ' '.join(section['title'].lower().split())
            relation = 'same_developer' if title.startswith('more by ') else 'similar_games' if title in ('similar games', 'similar apps') else None
            if relation:
                self.found.add(relation)
                if section['links']:
                    self.links[relation] = section['links'][0]
                self.results[relation].extend(section['packages'])


def parse_relationships(html):
    parser = Sections()
    parser.feed(html)
    return {key: list(dict.fromkeys(values)) for key, values in parser.results.items()}, parser.found


def section_links(html):
    parser = Sections()
    parser.feed(html)
    return parser.links


def expanded_apps(href, country, language, limit):
    """Follow the exact section link; the worker handles continuation tokens."""
    import json
    import subprocess
    from pathlib import Path
    parsed = urlparse(href)
    if parsed.hostname not in (None, 'play.google.com') or parsed.path not in ('/store/apps/dev', '/store/apps/developer', '/store/apps/collection/cluster'):
        raise ValueError('Unsupported Play Store section URL')
    result = subprocess.run(['node', str(Path(__file__).resolve().parents[1] / 'discovery-worker' / 'expand.mjs')],
                            input=json.dumps(dict(href=href, country=country, language=language, limit=limit)),
                            text=True, capture_output=True, timeout=150)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[-500:] or 'Pagination worker failed')
    return json.loads(result.stdout)
