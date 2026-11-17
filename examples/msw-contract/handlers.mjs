import { http, HttpResponse } from 'msw/http';

export const handlers = [
  http.get('http://petstore.swagger.io/v1/pets/:petId', ({ params }) => {
    if (params.petId === 'missing') return HttpResponse.json({ code: 404, message: 'No such pet' }, { status: 404 });
    // A realistic stale mock: the real API now specifies an integer, but URL params are strings.
    return HttpResponse.json({ id: params.petId, name: 'Milo' });
  }),
  http.get('http://petstore.swagger.io/v1/pets', () => HttpResponse.json([{ id: 1, name: 'Milo' }])),
];
