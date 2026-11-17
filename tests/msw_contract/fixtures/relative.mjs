import { http, HttpResponse } from 'msw/http';
export const handlers = [
  http.get('/pets/:id', ({ params }) => HttpResponse.json({ id: Number(params.id), name: 'Milo' })),
  http.head('/pets/:id', () => new HttpResponse(new ReadableStream({ pull() {} }), { headers: { 'content-type': 'application/json' } })),
];
