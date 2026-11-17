import { http, HttpResponse } from 'msw/http';
import { passthrough } from 'msw';
import { writeFile } from 'node:fs/promises';
import { request as httpRequest } from 'node:http';

let counter = 0;
export const handlers = [
  http.all('http://example.test/pets/:id', async ({ request, params }) => {
    const scenario = new URL(request.url).searchParams.get('scenario');
    switch (scenario) {
      case 'stale': return HttpResponse.json({ id: '1', name: 'Milo' });
      case 'missing': return HttpResponse.json({ id: 1 });
      case 'additional': return HttpResponse.json({ id: 1, name: 'Milo', oldField: true });
      case 'status': return HttpResponse.json({ id: 1, name: 'Milo' }, { status: 418 });
      case 'media': return HttpResponse.text('wrong content type');
      case 'missing-type': return new Response('{"id":1}', { headers: { 'content-type': '' } });
      case 'malformed': return new Response('{not json', { headers: { 'content-type': 'application/json' } });
      case 'throw': throw new Error('resolver exploded');
      case 'timeout': await new Promise(() => {}); break;
      case 'busy': while (true) {} // parent must terminate CPU-bound handlers too
      case 'passthrough': return passthrough();
      case 'network': return fetch('http://127.0.0.1:9/never');
      case 'http-network': httpRequest('http://127.0.0.1:9/never'); break;
      case 'write': await writeFile(new URL('./forbidden.txt', import.meta.url), 'should not be written'); break;
      case 'oversized': return HttpResponse.text('a'.repeat(4096));
      case 'logs': console.log('x'.repeat(9000)); return HttpResponse.json({ id: 1, name: 'Milo' });
      case 'error': return Response.error();
      case 'invalid-status': return HttpResponse.json({ code: 777, message: 'Internal non-HTTP code' }, { status: 777 });
      case 'invalid-utf8': return new Response(new Uint8Array([123, 34, 110, 97, 109, 101, 34, 58, 34, 255, 34, 125]), { headers: { 'content-type': 'application/json' } });
      case 'bom': return new Response('\uFEFF{"id":1,"name":"Milo"}', { headers: { 'content-type': 'application/json' } });
      case 'latin1': return new Response(new Uint8Array([99, 97, 102, 233]), { headers: { 'content-type': 'text/plain; charset=iso-8859-1' } });
      case 'bad-charset': return new Response('hello', { headers: { 'content-type': 'text/plain; charset=not-a-charset' } });
      case 'bad-stream': return new Response(new ReadableStream({ start(controller) { controller.enqueue('not bytes'); controller.close(); } }), { headers: { 'content-type': 'application/json' } });
      case 'abort': console.log(`WORKER_PID=${process.pid}`); await new Promise(() => {}); break;
      case 'unicode': return HttpResponse.json({ id: 1, name: '😀😀😀😀' });
      case 'regex': return HttpResponse.json({ id: 1, name: 'a'.repeat(32) + '!' });
      case 'fast-regex': return HttpResponse.json({ id: 1, name: 'aaa' });
      case 'undefined': return;
      case 'post': return HttpResponse.json({ ...(await request.json()), id: Number(params.id) });
      case 'nullable': return HttpResponse.json({ id: 1, name: null });
      case 'isolation': return HttpResponse.json({ id: ++counter, name: 'Milo' });
      default: return HttpResponse.json({ id: Number(params.id), name: 'Milo' });
    }
  }),
  http.get('http://example.test/problem', () => HttpResponse.json({ title: 42 }, { status: 400, headers: { 'content-type': 'application/problem+json; charset=utf-8' } })),
  http.get('http://example.test/text', () => HttpResponse.text('hello')),
  http.get('http://example.test/empty', () => new HttpResponse(null, { status: 204 })),
];
