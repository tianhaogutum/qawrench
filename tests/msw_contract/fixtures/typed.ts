import { http, HttpResponse, type HttpHandler } from 'msw';
const handlers: HttpHandler[] = [http.get('http://example.test/pets/:id', ({ params }) => HttpResponse.json({ id: Number(params.id), name: 'Milo' }))];
export default handlers;
