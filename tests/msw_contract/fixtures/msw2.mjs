import { http, HttpResponse } from 'msw2';
export const legacyHandlers = [http.get('http://example.test/pets/:id', ({ params }) => HttpResponse.json({ id: Number(params.id), name: 'Milo' }))];
