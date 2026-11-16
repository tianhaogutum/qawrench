"""Independent source coverage for the pinned extruct 0.18.0 selectors.

Keep these rules aligned with UPSTREAM.md, not a broader SEO interpretation.
"""

import re


JSONLD_SELECTOR = 'descendant-or-self::script[@type="application/ld+json"]'
OG_SELECTOR = '//head/meta[@property and @content] + recognized/declared namespace'
# extruct 0.18.0's built-in namespaces; declared prefixes use its regex too.
OG_NAMESPACES = {
    'og': 'http://ogp.me/ns#',
    **{name: f'http://ogp.me/ns/{name}#'
       for name in ('music', 'video', 'article', 'book', 'profile', 'product')},
}


def source_selection(tree, path, syntaxes):
    """Return diagnostics and independently expected nonuniform OG blocks."""
    coverage = {}
    if 'json-ld' in syntaxes:
        scripts = tree.xpath(JSONLD_SELECTOR)
        candidates = [s for s in tree.xpath('descendant-or-self::script')
                      if 'ld+json' in s.get('type', '').lower()]
        coverage['json-ld'] = {
            'selector': JSONLD_SELECTOR, 'candidates': len(candidates),
            'selected': len(scripts),
            'skipped': [{'file': str(path), 'line': s.sourceline,
                         'type': s.get('type'), 'reason': 'type_mismatch'}
                        for s in candidates if s not in scripts],
        }
    expected_og = []
    if 'opengraph' in syntaxes:
        def prefixes(node):
            return dict(re.findall(r'\s*(\w+):\s*([^\s]+)', node.get('prefix', '')))

        selected = []
        recognized = set()
        for head in tree.xpath('//head'):
            # Upstream uses document.head's parent, then this head's prefixes.
            parents = tree.head.xpath('parent::html')
            namespaces = prefixes(parents[0]) if parents else {}
            namespaces.update(prefixes(head))
            props = []
            for node in head.xpath('meta[@property and @content]'):
                prop = node.get('property')
                ns = prop.partition(':')[0]
                if ns in OG_NAMESPACES:
                    namespaces[ns] = OG_NAMESPACES[ns]
                if ns in namespaces:
                    recognized.add(node)
                    selected.append(node)
                    props.append((prop, node.get('content')))
            if props:
                expected_og.append({'namespace': namespaces, 'properties': props})
        candidates = tree.xpath('descendant-or-self::meta[@property or @name]')
        skipped = []
        for node in candidates:
            if node in recognized:
                continue
            if node.getparent().tag != 'head':
                reason = 'outside_head'
            elif 'property' not in node.attrib:
                reason = 'name_attribute'
            elif 'content' not in node.attrib:
                reason = 'missing_content'
            else:
                reason = 'unknown_namespace'
            skipped.append({'file': str(path), 'line': node.sourceline,
                            'property': node.get('property'), 'name': node.get('name'),
                            'reason': reason})
        coverage['opengraph'] = {
            'selector': OG_SELECTOR, 'candidates': len(candidates),
            'selected': len(selected), 'skipped': skipped,
        }
    return coverage, expected_og
