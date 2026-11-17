import { http, HttpResponse } from 'msw/http';
export const handlers = [http.get('http://petstore.swagger.io/v2/pet/:petId', ({ params }) => HttpResponse.json({ id: params.petId === 'stale' ? '1' : 1, name: 'Milo', photoUrls: [] }))];
