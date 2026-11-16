"""Independent, fixture-specific PyArrow readback; no exporter imports.

Checks examples/crawlee_export/evolution's five SYNTHETIC rows, including source-field presence.
Usage: python examples/crawlee_export/verify_evolution.py OUTPUT.parquet {dataset,jsonl}
"""
import hashlib
import json
from pathlib import Path
import sys

import pyarrow as pa
import pyarrow.parquet as pq


def main():
    path, mode = Path(sys.argv[1]), sys.argv[2]
    assert mode in ('dataset', 'jsonl')
    manifest = json.loads(Path(str(path) + '.manifest.json').read_text())
    expected = []
    for i, note, absent in [(1, None, ['note', 'score']), (2, None, ['score']),
                             (3, 'new — 東京\n', ['score']), (4, None, ['note', 'score']),
                             (5, '', ['score'])]:
        expected.append(dict(id=i, note=note, score=None, _cpe_missing_fields=absent,
                             _cpe_source=f'{i:09d}.json' if mode == 'dataset' else 'items.jsonl',
                             _cpe_item_id=str(i) if mode == 'dataset' else None,
                             _cpe_line=None if mode == 'dataset' else i))
    with pq.ParquetFile(path) as parquet:
        rows = parquet.read(use_threads=False).to_pylist()
        assert rows == expected, (rows, expected)
        assert parquet.metadata.num_rows == manifest['row_count'] == 5
        assert parquet.num_row_groups == manifest['row_groups']
        arrow = parquet.schema_arrow
        assert arrow.field('id').type == arrow.field('score').type == pa.int64()
        assert not arrow.field('id').nullable and arrow.field('score').nullable
        assert arrow.field('note').type == pa.string() and arrow.field('note').nullable
        assert arrow.field('_cpe_missing_fields') == pa.field(
            '_cpe_missing_fields', pa.list_(pa.field('element', pa.string(), nullable=False)),
            nullable=False)
        schema = json.loads(arrow.metadata[b'cpe.schema'])
        assert schema['version'] == 2 and schema['evolution'] == 'nullable-union'
        assert schema == manifest['schema']
    # Recompute each field's provenance from the physical Parquet columns.
    stats = {}
    for name in ('id', 'note', 'score'):
        present = [i for i, row in enumerate(rows, 1) if name not in row['_cpe_missing_fields']]
        stats[name] = dict(present=len(present), missing=len(rows) - len(present),
                           explicit_null=sum(rows[i-1][name] is None for i in present),
                           first_present_row=present[0] if present else None)
    assert stats == manifest['field_provenance'] == {
        'id': dict(present=5, missing=0, explicit_null=0, first_present_row=1),
        'note': dict(present=3, missing=2, explicit_null=1, first_present_row=2),
        'score': dict(present=0, missing=5, explicit_null=0, first_present_row=None)}
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                                 allow_nan=False).encode('utf-8') + b'\n')
    assert digest.hexdigest() == manifest['rows_sha256']
    assert path.stat().st_size == manifest['parquet_bytes']
    assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest['parquet_sha256']
    print('Verified 5 synthetic evolution rows: exact values, nullable types, field provenance and digests.')


if __name__ == '__main__':
    main()
