"""Versioned, exact-delta acceptance with immutable reference and coverage floors."""

from collections import Counter
import copy
import hashlib
from pathlib import Path
import re

from .core import ParityError, _strict_json, _string_policy, _token, _validate_base, _validate_pointers


METRICS = {
    'json-ld': ('selected', 'extracted_roots', 'entities', 'graph_wrappers'),
    'opengraph': ('selected', 'extracted_values', 'blocks'),
}
DELTA_FIELDS = {'syntax', 'scope', 'entity', 'kind', 'path', 'old', 'new'}


def _minimum_policy(requirements, syntaxes):
    if requirements is None:
        return {}
    if not isinstance(requirements, dict):
        raise ParityError('invalid_config', 'minimum_coverage must be a dict of metric names to nonnegative integer floors.')
    allowed = {f'{syntax}.{metric}' for syntax in syntaxes for metric in METRICS[syntax]}
    for metric, floor in requirements.items():
        if not isinstance(metric, str) or metric not in allowed:
            raise ParityError('invalid_config',
                              f'Unknown or unselected minimum coverage metric {metric!r}; choose from {sorted(allowed)!r}.')
        if type(floor) is not int or floor < 0:
            raise ParityError('invalid_config', f'Minimum coverage {metric} requires a nonnegative integer floor (not bool).')
    return dict(sorted(requirements.items()))


def _og_minimum_policy(requirements, syntaxes, *, field='minimum_og_properties'):
    if requirements is None:
        return {}
    if not isinstance(requirements, dict):
        raise ParityError('invalid_config', f'{field} must be a dict of exact property names to nonnegative integer floors.')
    if requirements and 'opengraph' not in syntaxes:
        raise ParityError('invalid_config', 'OpenGraph property minimums require selected opengraph syntax.')
    for name, floor in requirements.items():
        if not isinstance(name, str) or not name or any(char.isspace() for char in name):
            raise ParityError('invalid_config', 'OpenGraph property names must be nonempty exact strings without whitespace.')
        if type(floor) is not int or floor < 0:
            raise ParityError('invalid_config', f'OpenGraph property {name!r} requires a nonnegative integer floor (not bool).')
    return dict(sorted(requirements.items()))


def _entity_minimum_policy(requirements, syntaxes):
    if requirements is None:
        return []
    if (not isinstance(requirements, list)
            or any(not isinstance(identity, str) or not identity.strip() for identity in requirements)):
        raise ParityError('invalid_config', 'required_jsonld_entities must be a list of exact nonempty identity strings.')
    if requirements and 'json-ld' not in syntaxes:
        raise ParityError('invalid_config', 'Required JSON-LD entities require selected json-ld syntax.')
    if len(set(requirements)) != len(requirements):
        raise ParityError('invalid_config', 'Repeated required JSON-LD identity; choose each identity once.')
    return sorted(requirements)


def _type_minimum_policy(requirements, syntaxes):
    if requirements is None:
        return {}
    if not isinstance(requirements, dict):
        raise ParityError('invalid_config', 'required_jsonld_types must map exact entity identities to nonempty lists of literal types.')
    if requirements and 'json-ld' not in syntaxes:
        raise ParityError('invalid_config', 'Required JSON-LD types require selected json-ld syntax.')
    for identity, types in requirements.items():
        if (not isinstance(identity, str) or not identity.strip()
                or not isinstance(types, list) or not types
                or any(not isinstance(t, str) or not t.strip() for t in types)
                or len(set(types)) != len(types)):
            raise ParityError('invalid_config', 'Required JSON-LD types need nonblank exact identity/type strings and nonempty lists without duplicates.')
    return {key: sorted(types) for key, types in sorted(requirements.items())}


def _jsonld_nonblank_policy(requirements, syntaxes):
    if requirements is None:
        return {}
    if not isinstance(requirements, dict):
        raise ParityError('invalid_config', 'required_jsonld_nonblank_properties must map exact entity identities to nonempty lists of top-level property names.')
    if requirements and 'json-ld' not in syntaxes:
        raise ParityError('invalid_config', 'Nonblank JSON-LD property requirements need selected json-ld syntax.')
    for identity, names in requirements.items():
        if (not isinstance(identity, str) or not identity.strip()
                or not isinstance(names, list) or not names
                or any(not isinstance(name, str) or not name.strip() for name in names)
                or len(set(names)) != len(names)):
            raise ParityError('invalid_config', 'Nonblank JSON-LD requirements need exact nonblank identity/property strings and nonempty lists without duplicates.')
    return {key: sorted(names) for key, names in sorted(requirements.items())}


def _jsonld_array_policy(requirements, syntaxes, *, field='required_jsonld_nonempty_arrays',
                         label='Nonempty JSON-LD array requirements'):
    if requirements is None:
        return {}
    if not isinstance(requirements, dict):
        raise ParityError('invalid_config', f'{field} must map exact entity identities to nonempty lists of top-level property names.')
    if requirements and 'json-ld' not in syntaxes:
        raise ParityError('invalid_config', f'{label} need selected json-ld syntax.')
    for identity, names in requirements.items():
        if (not isinstance(identity, str) or not identity.strip()
                or not isinstance(names, list) or not names
                or any(not isinstance(name, str) or not name.strip() for name in names)
                or len(set(names)) != len(names)):
            raise ParityError('invalid_config', f'{label} need exact nonblank identity/property strings and nonempty lists without duplicates.')
    return {key: sorted(names) for key, names in sorted(requirements.items())}


def _jsonld_array_nonblank_policy(requirements, syntaxes, *, field='minimum_jsonld_array_nonblank_strings'):
    if requirements is None:
        return {}
    if not isinstance(requirements, dict):
        raise ParityError('invalid_config', f'{field} must map exact entity identities to nonempty property/count dicts.')
    if requirements and 'json-ld' not in syntaxes:
        raise ParityError('invalid_config', 'JSON-LD array string count requirements need selected json-ld syntax.')
    for identity, properties in requirements.items():
        if (not isinstance(identity, str) or not identity.strip()
                or not isinstance(properties, dict) or not properties):
            raise ParityError('invalid_config', 'JSON-LD array count requirements need exact nonblank identities and nonempty property/count dicts.')
        for name, floor in properties.items():
            if (not isinstance(name, str) or not name.strip()
                    or type(floor) is not int or floor < 0):
                raise ParityError('invalid_config', 'JSON-LD array count requirements need exact nonblank properties and nonnegative integers (not bool).')
    return {identity: dict(sorted(properties.items())) for identity, properties in sorted(requirements.items())}


def _jsonld_array_string_policy(requirements, syntaxes, *, field='required_jsonld_array_strings'):
    if requirements is None:
        return {}
    if not isinstance(requirements, dict):
        raise ParityError('invalid_config', f'{field} must map exact identities to nonempty property/member-list dicts.')
    if requirements and 'json-ld' not in syntaxes:
        raise ParityError('invalid_config', 'JSON-LD array string requirements need selected json-ld syntax.')
    for identity, properties in requirements.items():
        if (not isinstance(identity, str) or not identity.strip()
                or not isinstance(properties, dict) or not properties):
            raise ParityError('invalid_config', 'JSON-LD array string requirements need nonblank exact identities and nonempty property dicts.')
        for name, members in properties.items():
            if (not isinstance(name, str) or not name.strip()
                    or not isinstance(members, list) or not members
                    or any(not isinstance(member, str) or not member.strip() for member in members)
                    or len(set(members)) != len(members)):
                raise ParityError('invalid_config', 'JSON-LD array string requirements need nonblank exact properties/members and nonempty lists without duplicates.')
    return {identity: {name: sorted(members) for name, members in sorted(properties.items())}
            for identity, properties in sorted(requirements.items())}


def _guard_minimum(captures, requirements, og_requirements=None, entity_requirements=None,
                   og_nonblank_requirements=None, type_requirements=None, jsonld_nonblank_requirements=None,
                   jsonld_array_requirements=None, jsonld_array_nonblank_requirements=None,
                   jsonld_array_distinct_requirements=None, jsonld_array_string_requirements=None,
                   jsonld_array_allowed_requirements=None, jsonld_array_all_requirements=None):
    failures = []
    for side in ('old', 'new'):
        capture = captures[side]
        unmet = []
        for metric, required in sorted(requirements.items()):
            syntax, name = metric.split('.')
            observed = capture['coverage'][syntax][name]
            if observed < required:
                unmet.append({'metric': metric, 'required': required, 'observed': observed})
        for name, required in sorted((og_requirements or {}).items()):
            observed = capture['coverage']['opengraph']['property_counts'].get(name, 0)
            if observed < required:
                unmet.append({'property': name, 'required': required, 'observed': observed})
        if og_nonblank_requirements:
            # Historical captures lack these counts; totals cannot tell whether
            # a value was nonblank, even when the requested floor is zero.
            counts = capture['coverage']['opengraph'].get('nonblank_property_counts')
            if counts is None:
                raise ParityError('invalid_config', 'Nonblank OpenGraph minimums require captured nonblank_property_counts.')
            for name, required in sorted(og_nonblank_requirements.items()):
                observed = counts.get(name, 0)
                if observed < required:
                    unmet.append({'property': name, 'value_filter': 'nonblank',
                                  'required': required, 'observed': observed})
        if entity_requirements:
            identities = set(capture['coverage']['json-ld']['entity_ids'])
            for identity in sorted(entity_requirements):
                if identity not in identities:
                    unmet.append({'entity': identity, 'required': 1, 'observed': 0})
        if type_requirements:
            types = capture['coverage']['json-ld'].get('entity_types')
            if types is None:
                raise ParityError('invalid_config', 'JSON-LD type requirements need captured entity_types; types cannot be inferred from identities.')
            for identity, required_types in sorted(type_requirements.items()):
                observed = types.get(identity, [])
                for required in sorted(required_types):
                    if required not in observed:
                        unmet.append({'entity': identity, 'type': required, 'required': 1,
                                      'observed': 0, 'observed_types': observed})
        if jsonld_nonblank_requirements:
            properties = capture['coverage']['json-ld'].get('entity_nonblank_properties')
            if properties is None:
                raise ParityError('invalid_config', 'Nonblank JSON-LD requirements need captured entity_nonblank_properties; presence cannot be inferred from identities or types.')
            for identity, names in sorted(jsonld_nonblank_requirements.items()):
                for name in sorted(names):
                    if name not in properties.get(identity, []):
                        unmet.append({'entity': identity, 'property': name, 'value_filter': 'nonblank',
                                      'required': 1, 'observed': 0})
        if jsonld_array_requirements:
            shapes = capture['coverage']['json-ld'].get('entity_property_shapes')
            if shapes is None:
                raise ParityError('invalid_config', 'Nonempty JSON-LD array requirements need captured entity_property_shapes; original array presence cannot be inferred.')
            for identity, names in sorted(jsonld_array_requirements.items()):
                for name in sorted(names):
                    shape = shapes.get(identity, {}).get(name, {'type': 'missing'})
                    if shape['type'] != 'array' or shape['length'] < 1:
                        unmet.append({'entity': identity, 'property': name, 'value_filter': 'nonempty-array',
                                      'required': 1, 'observed': 0, 'observed_type': shape['type'],
                                      **({'observed_length': shape['length']} if shape['type'] == 'array' else {})})
        for array_requirements, count_field, value_filter in (
                (jsonld_array_nonblank_requirements, 'entity_array_nonblank_string_counts', 'array-nonblank-string'),
                (jsonld_array_distinct_requirements, 'entity_array_distinct_nonblank_string_counts', 'array-distinct-nonblank-string')):
            if not array_requirements:
                continue
            coverage = capture['coverage']['json-ld']
            counts = coverage.get(count_field)
            shapes = coverage.get('entity_property_shapes')
            if counts is None or shapes is None:
                raise ParityError('invalid_config', f'JSON-LD array string minimums need captured {count_field} and entity_property_shapes; counts cannot be inferred.')
            for identity, properties in sorted(array_requirements.items()):
                for name, required in sorted(properties.items()):
                    observed_type = shapes.get(identity, {}).get(name, {'type': 'missing'})['type']
                    observed = counts.get(identity, {}).get(name, 0)
                    # A zero floor still asserts original array shape.
                    if observed_type != 'array' or observed < required:
                        unmet.append({'entity': identity, 'property': name, 'value_filter': value_filter,
                                      'required': required, 'observed': observed, 'observed_type': observed_type})
        if jsonld_array_string_requirements:
            coverage = capture['coverage']['json-ld']
            members = coverage.get('entity_array_nonblank_string_members')
            shapes = coverage.get('entity_property_shapes')
            if members is None or shapes is None:
                raise ParityError('invalid_config', 'JSON-LD array string requirements need captured entity_array_nonblank_string_members and entity_property_shapes; original members cannot be inferred.')
            for identity, properties in sorted(jsonld_array_string_requirements.items()):
                for name, required_members in sorted(properties.items()):
                    observed_type = shapes.get(identity, {}).get(name, {'type': 'missing'})['type']
                    observed = members.get(identity, {}).get(name, [])
                    for member in sorted(required_members):
                        if observed_type != 'array' or member not in observed:
                            unmet.append({'entity': identity, 'property': name, 'member': member,
                                          'value_filter': 'array-string-member', 'required': 1, 'observed': 0,
                                          'observed_type': observed_type, 'observed_members': observed})
        if jsonld_array_allowed_requirements:
            coverage = capture['coverage']['json-ld']
            members = coverage.get('entity_array_nonblank_string_members')
            shapes = coverage.get('entity_property_shapes')
            if members is None or shapes is None:
                raise ParityError('invalid_config', 'JSON-LD array string allowlists need captured entity_array_nonblank_string_members and entity_property_shapes; original members cannot be inferred.')
            for identity, properties in sorted(jsonld_array_allowed_requirements.items()):
                for name, allowed_members in sorted(properties.items()):
                    observed_type = shapes.get(identity, {}).get(name, {'type': 'missing'})['type']
                    observed = members.get(identity, {}).get(name, [])
                    unexpected = sorted(set(observed) - set(allowed_members))
                    # Empty arrays pass a subset constraint; nonarrays never do.
                    if observed_type != 'array' or unexpected:
                        unmet.append({'entity': identity, 'property': name,
                                      'value_filter': 'array-allowed-string-members',
                                      'required': 0, 'observed': len(unexpected), 'observed_type': observed_type,
                                      'unexpected_members': unexpected, 'allowed_members': allowed_members,
                                      'observed_members': observed})
        if jsonld_array_all_requirements:
            coverage = capture['coverage']['json-ld']
            counts = coverage.get('entity_array_nonblank_string_counts')
            shapes = coverage.get('entity_property_shapes')
            if counts is None or shapes is None:
                raise ParityError('invalid_config', 'All-elements JSON-LD array requirements need captured entity_array_nonblank_string_counts and entity_property_shapes; original counts/lengths cannot be inferred. Use reviewed recapture.')
            for identity, names in sorted(jsonld_array_all_requirements.items()):
                for name in sorted(names):
                    shape = shapes.get(identity, {}).get(name, {'type': 'missing'})
                    length = shape.get('length')
                    observed = counts.get(identity, {}).get(name, 0)
                    # Universal constraint: [] passes; duplicates remain separate
                    # original elements, independently of normalization/set policy.
                    if shape['type'] != 'array' or observed != length:
                        unmet.append({'entity': identity, 'property': name,
                                      'value_filter': 'array-all-nonblank-strings', 'required': 'all',
                                      'observed': observed, 'observed_type': shape['type'],
                                      'observed_length': length})
        if unmet:
            failures.append({'capture': side, 'file': capture['file'], 'unmet': unmet,
                             'coverage': capture['coverage']})
    if failures:
        detail = '; '.join(f"{p['capture']} {m.get('metric', m.get('property', m.get('entity')))}"
                           + (f" type {m['type']!r}" if 'type' in m else '')
                           + (f" entity {m['entity']!r}" if 'entity' in m and 'property' in m else '')
                           + (f" member {m['member']!r}" if 'member' in m else '')
                           + (f" unexpected members {m['unexpected_members']!r}, allowed {m['allowed_members']!r}" if 'unexpected_members' in m else '')
                           + f": required {m['required']}, observed {m['observed']}"
                           for p in failures for m in p['unmet'])
        raise ParityError('minimum_coverage', 'Minimum metadata coverage not met: ' + detail
                          + '. Inspect capture selector diagnostics before saving or accepting a baseline.', failures)


def _guard_maximum(captures, requirements):
    if not requirements:
        return
    failures = []
    for side in ('old', 'new'):
        capture = captures[side]
        coverage = capture['coverage']['json-ld']
        counts = coverage.get('entity_array_nonblank_string_counts')
        shapes = coverage.get('entity_property_shapes')
        if counts is None or shapes is None:
            raise ParityError('invalid_config', 'JSON-LD array string maximums need captured entity_array_nonblank_string_counts and entity_property_shapes; counts cannot be inferred.')
        unmet = []
        for identity, properties in sorted(requirements.items()):
            for name, maximum in sorted(properties.items()):
                observed_type = shapes.get(identity, {}).get(name, {'type': 'missing'})['type']
                observed = counts.get(identity, {}).get(name, 0)
                # Zero still asserts original array shape, independently of counts.
                if observed_type != 'array' or observed > maximum:
                    unmet.append({'entity': identity, 'property': name, 'value_filter': 'array-nonblank-string',
                                  'maximum': maximum, 'observed': observed, 'observed_type': observed_type})
        if unmet:
            failures.append({'capture': side, 'file': capture['file'], 'unmet': unmet,
                             'coverage': capture['coverage']})
    if failures:
        detail = '; '.join(f"{p['capture']} {m['property']} entity {m['entity']!r}: maximum {m['maximum']}, "
                           f"observed {m['observed']} (type {m['observed_type']})"
                           for p in failures for m in p['unmet'])
        raise ParityError('maximum_coverage', 'Maximum metadata coverage not met: ' + detail
                          + '. Inspect capture selector diagnostics before saving or accepting a baseline.', failures)


def _delta(change):
    # Capture paths/line numbers may move; data, scope, identity and kind may not.
    return {key: value for key, value in change.items() if key in DELTA_FIELDS}


def _coverage_counts(capture, syntaxes):
    coverage = capture['coverage']
    if not isinstance(coverage, dict) or set(coverage) != set(syntaxes):
        raise ValueError('coverage must describe every selected syntax')
    result = {}
    for syntax in syntaxes:
        for metric in METRICS[syntax]:
            count = coverage[syntax][metric]
            if type(count) is not int or count < 0:
                raise ValueError(f'invalid count {syntax}.{metric}')
            result[f'{syntax}.{metric}'] = count
        if syntax == 'json-ld':
            ids = coverage[syntax]['entity_ids']
            if (not isinstance(ids, list) or any(not isinstance(i, str) or not i for i in ids)
                    or len(set(ids)) != len(ids) or len(ids) != coverage[syntax]['entities']):
                raise ValueError('invalid entity coverage')
            result.update({f'json-ld.entity:{identity}': 1 for identity in ids})
            if 'entity_types' in coverage[syntax]:
                types = coverage[syntax]['entity_types']
                if (not isinstance(types, dict) or set(types) != set(ids)
                        or any(not isinstance(ts, list)
                               or any(not isinstance(t, str) or not t.strip() for t in ts)
                               or len(set(ts)) != len(ts) for ts in types.values())):
                    raise ValueError('invalid entity type coverage')
            # Literal types create no implicit coverage-loss floors.
            if 'entity_nonblank_properties' in coverage[syntax]:
                properties = coverage[syntax]['entity_nonblank_properties']
                if (not isinstance(properties, dict) or set(properties) != set(ids)
                        or any(not isinstance(names, list)
                               or any(not isinstance(name, str) for name in names)
                               or len(set(names)) != len(names) for names in properties.values())):
                    raise ValueError('invalid entity nonblank property coverage')
            # Raw top-level string presence supports explicit requirements only.
            if 'entity_property_shapes' in coverage[syntax]:
                shapes = coverage[syntax]['entity_property_shapes']
                if not isinstance(shapes, dict) or set(shapes) != set(ids):
                    raise ValueError('invalid entity property shape coverage')
                for properties in shapes.values():
                    if not isinstance(properties, dict):
                        raise ValueError('invalid entity property shape coverage')
                    for name, shape in properties.items():
                        if (not isinstance(name, str) or not isinstance(shape, dict)
                                or not isinstance(shape.get('type'), str)
                                or shape['type'] not in {'array', 'object', 'string', 'number', 'boolean', 'null'}
                                or set(shape) != ({'type', 'length'} if shape['type'] == 'array' else {'type'})
                                or (shape['type'] == 'array'
                                    and (type(shape['length']) is not int or shape['length'] < 0))):
                            raise ValueError('invalid entity property shape coverage')
            # Shallow original types/lengths create no automatic coverage floors.
            for field in ('entity_array_nonblank_string_counts', 'entity_array_distinct_nonblank_string_counts'):
                if field not in coverage[syntax]:
                    continue
                counts = coverage[syntax][field]
                shapes = coverage[syntax].get('entity_property_shapes')
                if not isinstance(counts, dict) or set(counts) != set(ids) or shapes is None:
                    raise ValueError(f'invalid {field} coverage')
                for identity, properties in counts.items():
                    arrays = {name: shape['length'] for name, shape in shapes[identity].items()
                              if shape['type'] == 'array'}
                    if (not isinstance(properties, dict) or set(properties) != set(arrays)
                            or any(type(count) is not int or not 0 <= count <= arrays[name]
                                   for name, count in properties.items())):
                        raise ValueError(f'invalid {field} coverage')
                    if (field == 'entity_array_distinct_nonblank_string_counts'
                            and 'entity_array_nonblank_string_counts' in coverage[syntax]):
                        totals = coverage[syntax]['entity_array_nonblank_string_counts'][identity]
                        if any(count > totals[name] or (count == 0) != (totals[name] == 0)
                               for name, count in properties.items()):
                            raise ValueError('distinct string counts must agree with nonblank string totals')
            if 'entity_array_nonblank_string_members' in coverage[syntax]:
                members = coverage[syntax]['entity_array_nonblank_string_members']
                shapes = coverage[syntax].get('entity_property_shapes')
                if not isinstance(members, dict) or set(members) != set(ids) or shapes is None:
                    raise ValueError('invalid entity array string membership coverage')
                for identity, properties in members.items():
                    arrays = {name: shape['length'] for name, shape in shapes[identity].items()
                              if shape['type'] == 'array'}
                    if not isinstance(properties, dict) or set(properties) != set(arrays):
                        raise ValueError('invalid entity array string membership properties')
                    for name, values in properties.items():
                        if (not isinstance(values, list)
                                or any(not isinstance(v, str) or not v.strip() for v in values)
                                or len(set(values)) != len(values) or len(values) > arrays[name]):
                            raise ValueError('invalid original nonblank string members')
                        distinct = coverage[syntax].get('entity_array_distinct_nonblank_string_counts')
                        totals = coverage[syntax].get('entity_array_nonblank_string_counts')
                        if distinct is not None and len(values) != distinct[identity][name]:
                            raise ValueError('string members must agree with distinct counts')
                        if totals is not None and (len(values) > totals[identity][name]
                                                  or (not values) != (totals[identity][name] == 0)):
                            raise ValueError('string members must agree with multiplicity counts')
            # Original counts and members support explicit requirements only.
        else:
            counts = coverage[syntax]['property_counts']
            if (not isinstance(counts, dict)
                    or any(type(v) is not int or v < 1 for v in counts.values())
                    or sum(counts.values()) != coverage[syntax]['extracted_values']):
                raise ValueError('invalid property coverage')
            result.update({f'opengraph.property:{key}': count for key, count in counts.items()})
            if 'nonblank_property_counts' in coverage[syntax]:
                nonblank = coverage[syntax]['nonblank_property_counts']
                if (not isinstance(nonblank, dict) or set(nonblank) != set(counts)
                        or any(type(v) is not int or not 0 <= v <= counts[k] for k, v in nonblank.items())):
                    raise ValueError('invalid nonblank property coverage')
            # Nonblank counts support explicit requirements only. They do not
            # change the automatic floors of older baseline versions.
    return result


def _guard_coverage(expected, current, syntaxes):
    floors = _coverage_counts(expected, syntaxes)
    actual = _coverage_counts(current, syntaxes)
    losses = [f'{key}: {count} -> {actual.get(key, 0)}' for key, count in floors.items()
              if actual.get(key, 0) < count]
    if losses:
        raise ParityError('coverage_regression',
                          'Selected/extracted metadata coverage decreased: ' + '; '.join(losses)
                          + '. Inspect the capture and selector diagnostics; do not accept as field noise.',
                          [{'file': current['file'], 'coverage': current['coverage']}])


def make_baseline(report: dict, *, minimum_coverage: dict[str, int] | None = None,
                  minimum_og_properties: dict[str, int] | None = None,
                  required_jsonld_entities: list[str] | None = None,
                  minimum_og_nonblank_properties: dict[str, int] | None = None,
                  required_jsonld_types: dict[str, list[str]] | None = None,
                  required_jsonld_nonblank_properties: dict[str, list[str]] | None = None,
                  required_jsonld_nonempty_arrays: dict[str, list[str]] | None = None,
                  minimum_jsonld_array_nonblank_strings: dict[str, dict[str, int]] | None = None,
                  minimum_jsonld_array_distinct_nonblank_strings: dict[str, dict[str, int]] | None = None,
                  required_jsonld_array_strings: dict[str, dict[str, list[str]]] | None = None,
                  allowed_jsonld_array_strings: dict[str, dict[str, list[str]]] | None = None,
                  maximum_jsonld_array_nonblank_strings: dict[str, dict[str, int]] | None = None,
                  required_jsonld_array_all_nonblank_strings: dict[str, list[str]] | None = None) -> dict:
    """Create a serializable baseline from an unfiltered compare_files report.

    Coverage loss across the pair cannot become accepted noise. The caller owns
    persistence; the CLI uses atomic replacement only after this check succeeds.
    Optional syntax.metric / OpenGraph total or nonblank floors and exact JSON-LD
    identity/literal type/nonblank string/nonempty array requirements and array
    nonblank string element multiplicity/distinct floors and exact original string
    membership requirements / allowed-member subset constraints apply to both captures
    and immediate nonblank string multiplicity ceilings persist for replay.
    Allowlists imply neither required membership nor count bounds. Optional
    all-elements requirements compare original qualifying multiplicity to length;
    empty arrays pass independently of nonempty/minimum guards.
    """
    if report.get('status') not in ('equal', 'different') or 'baseline' in report:
        raise ParityError('invalid_config', 'Save baselines only from successful, unfiltered comparisons.')
    requirements = _minimum_policy(minimum_coverage, report['policy']['syntaxes'])
    og_requirements = _og_minimum_policy(minimum_og_properties, report['policy']['syntaxes'])
    entity_requirements = _entity_minimum_policy(required_jsonld_entities, report['policy']['syntaxes'])
    og_nonblank_requirements = _og_minimum_policy(minimum_og_nonblank_properties, report['policy']['syntaxes'],
                                                 field='minimum_og_nonblank_properties')
    type_requirements = _type_minimum_policy(required_jsonld_types, report['policy']['syntaxes'])
    jsonld_nonblank_requirements = _jsonld_nonblank_policy(required_jsonld_nonblank_properties, report['policy']['syntaxes'])
    jsonld_array_requirements = _jsonld_array_policy(required_jsonld_nonempty_arrays, report['policy']['syntaxes'])
    jsonld_array_nonblank_requirements = _jsonld_array_nonblank_policy(minimum_jsonld_array_nonblank_strings, report['policy']['syntaxes'])
    jsonld_array_distinct_requirements = _jsonld_array_nonblank_policy(
        minimum_jsonld_array_distinct_nonblank_strings, report['policy']['syntaxes'],
        field='minimum_jsonld_array_distinct_nonblank_strings')
    jsonld_array_string_requirements = _jsonld_array_string_policy(required_jsonld_array_strings, report['policy']['syntaxes'])
    jsonld_array_allowed_requirements = _jsonld_array_string_policy(
        allowed_jsonld_array_strings, report['policy']['syntaxes'], field='allowed_jsonld_array_strings')
    jsonld_array_maximum_requirements = _jsonld_array_nonblank_policy(
        maximum_jsonld_array_nonblank_strings, report['policy']['syntaxes'],
        field='maximum_jsonld_array_nonblank_strings')
    jsonld_array_all_requirements = _jsonld_array_policy(
        required_jsonld_array_all_nonblank_strings, report['policy']['syntaxes'],
        field='required_jsonld_array_all_nonblank_strings', label='All-elements JSON-LD array requirements')
    _guard_minimum(report['captures'], requirements, og_requirements, entity_requirements,
                   og_nonblank_requirements, type_requirements, jsonld_nonblank_requirements, jsonld_array_requirements,
                   jsonld_array_nonblank_requirements, jsonld_array_distinct_requirements, jsonld_array_string_requirements,
                   jsonld_array_allowed_requirements, jsonld_array_all_requirements)
    _guard_maximum(report['captures'], jsonld_array_maximum_requirements)
    _guard_coverage(report['captures']['old'], report['captures']['new'], report['policy']['syntaxes'])
    return copy.deepcopy({
        'baseline_version': 1, 'tool': report['tool'], 'extractor': report['extractor'],
        'base_url': report['base_url'], 'policy': report['policy'],
        'captures': report['captures'], 'accepted_changes': [_delta(c) for c in report['changes']],
        **({'minimum_coverage': requirements} if requirements else {}),
        **({'minimum_og_properties': og_requirements} if og_requirements else {}),
        **({'required_jsonld_entities': entity_requirements} if entity_requirements else {}),
        **({'minimum_og_nonblank_properties': og_nonblank_requirements} if og_nonblank_requirements else {}),
        **({'required_jsonld_types': type_requirements} if type_requirements else {}),
        **({'required_jsonld_nonblank_properties': jsonld_nonblank_requirements} if jsonld_nonblank_requirements else {}),
        **({'required_jsonld_nonempty_arrays': jsonld_array_requirements} if jsonld_array_requirements else {}),
        **({'minimum_jsonld_array_nonblank_strings': jsonld_array_nonblank_requirements} if jsonld_array_nonblank_requirements else {}),
        **({'minimum_jsonld_array_distinct_nonblank_strings': jsonld_array_distinct_requirements} if jsonld_array_distinct_requirements else {}),
        **({'required_jsonld_array_strings': jsonld_array_string_requirements} if jsonld_array_string_requirements else {}),
        **({'allowed_jsonld_array_strings': jsonld_array_allowed_requirements} if jsonld_array_allowed_requirements else {}),
        **({'maximum_jsonld_array_nonblank_strings': jsonld_array_maximum_requirements} if jsonld_array_maximum_requirements else {}),
        **({'required_jsonld_array_all_nonblank_strings': jsonld_array_all_requirements} if jsonld_array_all_requirements else {}),
    })


def _read_baseline(path):
    try:
        data = path.read_bytes()
        value = _strict_json(data.decode('utf-8-sig'), {'file': str(path)})
        fields = {
            'baseline_version', 'tool', 'extractor', 'base_url', 'policy', 'captures', 'accepted_changes'}
        if (not isinstance(value, dict) or set(value) - {
                'minimum_coverage', 'minimum_og_properties', 'required_jsonld_entities',
                'minimum_og_nonblank_properties', 'required_jsonld_types', 'required_jsonld_nonblank_properties',
                'required_jsonld_nonempty_arrays', 'minimum_jsonld_array_nonblank_strings',
                'minimum_jsonld_array_distinct_nonblank_strings', 'required_jsonld_array_strings',
                'allowed_jsonld_array_strings', 'maximum_jsonld_array_nonblank_strings',
                'required_jsonld_array_all_nonblank_strings'} != fields
                or type(value['baseline_version']) is not int or value['baseline_version'] != 1
                or value['tool'] != 'structured-data-parity'
                or not isinstance(value['extractor'], str) or not isinstance(value['base_url'], str)):
            raise ValueError('expected a structured-data-parity baseline version 1')
        policy = value['policy']
        if (not isinstance(policy, dict) or set(policy) != {
                'syntaxes', 'identity_field', 'relative_id', 'set_valued', 'normalize'}
                or not isinstance(policy['syntaxes'], list) or not policy['syntaxes']
                or any(s not in METRICS for s in policy['syntaxes'])
                or len(set(policy['syntaxes'])) != len(policy['syntaxes'])):
            raise ValueError('invalid comparison policy')
        if (not isinstance(policy['identity_field'], str) or not policy['identity_field'].strip()
                or policy['relative_id'] != 'resolve-for-identity-only'
                or not isinstance(policy['set_valued'], list)
                or not isinstance(policy['normalize'], dict)):
            raise ValueError('invalid comparison policy fields')
        _validate_base(value['base_url'])
        _validate_pointers(tuple(policy['set_valued']))
        _string_policy(policy['normalize'], policy['identity_field'], frozenset(policy['set_valued']))
        if 'minimum_coverage' in value and not isinstance(value['minimum_coverage'], dict):
            raise ValueError('minimum_coverage must be a dict when present')
        requirements = _minimum_policy(value.get('minimum_coverage'), policy['syntaxes'])
        if 'minimum_og_properties' in value and not isinstance(value['minimum_og_properties'], dict):
            raise ValueError('minimum_og_properties must be a dict when present')
        og_requirements = _og_minimum_policy(value.get('minimum_og_properties'), policy['syntaxes'])
        if 'required_jsonld_entities' in value and not isinstance(value['required_jsonld_entities'], list):
            raise ValueError('required_jsonld_entities must be a list when present')
        entity_requirements = _entity_minimum_policy(value.get('required_jsonld_entities'), policy['syntaxes'])
        if 'minimum_og_nonblank_properties' in value and not isinstance(value['minimum_og_nonblank_properties'], dict):
            raise ValueError('minimum_og_nonblank_properties must be a dict when present')
        og_nonblank_requirements = _og_minimum_policy(value.get('minimum_og_nonblank_properties'), policy['syntaxes'],
                                                     field='minimum_og_nonblank_properties')
        if 'required_jsonld_types' in value and not isinstance(value['required_jsonld_types'], dict):
            raise ValueError('required_jsonld_types must be a dict when present')
        type_requirements = _type_minimum_policy(value.get('required_jsonld_types'), policy['syntaxes'])
        if 'required_jsonld_nonblank_properties' in value and not isinstance(value['required_jsonld_nonblank_properties'], dict):
            raise ValueError('required_jsonld_nonblank_properties must be a dict when present')
        jsonld_nonblank_requirements = _jsonld_nonblank_policy(value.get('required_jsonld_nonblank_properties'), policy['syntaxes'])
        if 'required_jsonld_nonempty_arrays' in value and not isinstance(value['required_jsonld_nonempty_arrays'], dict):
            raise ValueError('required_jsonld_nonempty_arrays must be a dict when present')
        jsonld_array_requirements = _jsonld_array_policy(value.get('required_jsonld_nonempty_arrays'), policy['syntaxes'])
        if 'minimum_jsonld_array_nonblank_strings' in value and not isinstance(value['minimum_jsonld_array_nonblank_strings'], dict):
            raise ValueError('minimum_jsonld_array_nonblank_strings must be a dict when present')
        jsonld_array_nonblank_requirements = _jsonld_array_nonblank_policy(value.get('minimum_jsonld_array_nonblank_strings'), policy['syntaxes'])
        if 'minimum_jsonld_array_distinct_nonblank_strings' in value and not isinstance(value['minimum_jsonld_array_distinct_nonblank_strings'], dict):
            raise ValueError('minimum_jsonld_array_distinct_nonblank_strings must be a dict when present')
        jsonld_array_distinct_requirements = _jsonld_array_nonblank_policy(
            value.get('minimum_jsonld_array_distinct_nonblank_strings'), policy['syntaxes'],
            field='minimum_jsonld_array_distinct_nonblank_strings')
        if 'required_jsonld_array_strings' in value and not isinstance(value['required_jsonld_array_strings'], dict):
            raise ValueError('required_jsonld_array_strings must be a dict when present')
        jsonld_array_string_requirements = _jsonld_array_string_policy(value.get('required_jsonld_array_strings'), policy['syntaxes'])
        if 'allowed_jsonld_array_strings' in value and not isinstance(value['allowed_jsonld_array_strings'], dict):
            raise ValueError('allowed_jsonld_array_strings must be a dict when present')
        jsonld_array_allowed_requirements = _jsonld_array_string_policy(
            value.get('allowed_jsonld_array_strings'), policy['syntaxes'], field='allowed_jsonld_array_strings')
        if 'maximum_jsonld_array_nonblank_strings' in value and not isinstance(value['maximum_jsonld_array_nonblank_strings'], dict):
            raise ValueError('maximum_jsonld_array_nonblank_strings must be a dict when present')
        jsonld_array_maximum_requirements = _jsonld_array_nonblank_policy(
            value.get('maximum_jsonld_array_nonblank_strings'), policy['syntaxes'],
            field='maximum_jsonld_array_nonblank_strings')
        if 'required_jsonld_array_all_nonblank_strings' in value and not isinstance(value['required_jsonld_array_all_nonblank_strings'], dict):
            raise ValueError('required_jsonld_array_all_nonblank_strings must be a dict when present')
        jsonld_array_all_requirements = _jsonld_array_policy(
            value.get('required_jsonld_array_all_nonblank_strings'), policy['syntaxes'],
            field='required_jsonld_array_all_nonblank_strings', label='All-elements JSON-LD array requirements')
        if not isinstance(value['captures'], dict) or set(value['captures']) != {'old', 'new'}:
            raise ValueError('expected old/new capture hashes and coverage')
        for capture in value['captures'].values():
            if (not isinstance(capture['file'], str) or not isinstance(capture['sha256'], str)
                    or not re.fullmatch('[0-9a-f]{64}', capture['sha256'])):
                raise ValueError('invalid capture file/hash')
            _coverage_counts(capture, policy['syntaxes'])
        changes = value['accepted_changes']
        if not isinstance(changes, list):
            raise ValueError('accepted_changes must be a list of exact deltas')
        for change in changes:
            if not isinstance(change, dict):
                raise ValueError('accepted delta must be an object')
            kind = change.get('kind')
            fields = {'changed': {'old', 'new'}, 'added': {'new'}, 'removed': {'old'},
                      'added_entity': {'new'}, 'removed_entity': {'old'}}.get(kind)
            if (fields is None or set(change) != {'syntax', 'scope', 'entity', 'kind', 'path'} | fields
                    or change['syntax'] not in policy['syntaxes']
                    or (change['syntax'], change['scope']) not in {
                        ('json-ld', 'entity'), ('json-ld', 'graph_metadata'),
                        ('opengraph', 'properties'), ('opengraph', 'namespaces')}
                    or not isinstance(change['path'], str)
                    or (change['path'] and not change['path'].startswith('/'))
                    or re.search(r'~(?![01])', change['path'])
                    or (change['scope'] == 'entity' and not isinstance(change['entity'], str))
                    or (change['scope'] != 'entity' and change['entity'] is not None)):
                raise ValueError('invalid exact accepted delta')
        # A hand-edited baseline must not bypass the creation-time coverage guard.
        _guard_minimum(value['captures'], requirements, og_requirements, entity_requirements,
                       og_nonblank_requirements, type_requirements, jsonld_nonblank_requirements, jsonld_array_requirements,
                   jsonld_array_nonblank_requirements, jsonld_array_distinct_requirements, jsonld_array_string_requirements,
                   jsonld_array_allowed_requirements, jsonld_array_all_requirements)
        _guard_maximum(value['captures'], jsonld_array_maximum_requirements)
        _guard_coverage(value['captures']['old'], value['captures']['new'], policy['syntaxes'])
        return value, hashlib.sha256(data).hexdigest()
    except (OSError, ValueError, TypeError, KeyError, ParityError, RecursionError) as exc:
        raise ParityError('invalid_baseline', f'Cannot use baseline {path}: {exc}', [{'file': str(path)}]) from exc


def apply_baseline(report: dict, path: Path) -> dict:
    baseline, digest = _read_baseline(path)
    for key in ('tool', 'extractor', 'base_url', 'policy'):
        if _token(baseline[key]) != _token(report[key]):
            raise ParityError('baseline_mismatch', f'Baseline {key} does not match this comparison.', [{'file': str(path)}])
    if baseline['captures']['old']['sha256'] != report['captures']['old']['sha256']:
        raise ParityError('baseline_mismatch', 'Reference HTML SHA-256 differs from the saved baseline; use the original reference or explicitly save a new baseline.', [{'file': report['captures']['old']['file']}])
    _guard_minimum(report['captures'], baseline.get('minimum_coverage', {}),
                   baseline.get('minimum_og_properties', {}), baseline.get('required_jsonld_entities', []),
                   baseline.get('minimum_og_nonblank_properties', {}), baseline.get('required_jsonld_types', {}),
                   baseline.get('required_jsonld_nonblank_properties', {}), baseline.get('required_jsonld_nonempty_arrays', {}),
                   baseline.get('minimum_jsonld_array_nonblank_strings', {}),
                   baseline.get('minimum_jsonld_array_distinct_nonblank_strings', {}),
                   baseline.get('required_jsonld_array_strings', {}),
                   baseline.get('allowed_jsonld_array_strings', {}),
                   baseline.get('required_jsonld_array_all_nonblank_strings', {}))
    _guard_maximum(report['captures'], baseline.get('maximum_jsonld_array_nonblank_strings', {}))
    for side in ('old', 'new'):
        _guard_coverage(baseline['captures'][side], report['captures'][side], report['policy']['syntaxes'])
    allowed = Counter(_token(c) for c in baseline['accepted_changes'])
    remaining, accepted = [], []
    for change in report['changes']:
        token = _token(_delta(change))
        if allowed[token]:
            allowed[token] -= 1
            accepted.append(change)
        else:
            remaining.append(change)
    result = copy.deepcopy(report)
    result.update(baseline={'file': str(path), 'sha256': digest},
                  observed_change_count=len(report['changes']),
                  accepted_change_count=len(accepted), accepted_changes=accepted,
                  changes=remaining, change_count=len(remaining),
                  status='different' if remaining else 'equal')
    return result
