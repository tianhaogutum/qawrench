import { readFile } from 'node:fs/promises';
import { parse } from 'yaml';
import { fromOpenApi } from '@msw/source/open-api';

export const handlers = await fromOpenApi(parse(await readFile(new URL('./upstream/petstore-3.0.yaml', import.meta.url), 'utf8')));
