const { http, HttpResponse } = require('msw2');
module.exports = { handlers: [http.get('http://example.test/pets/:id', ({ params }) => HttpResponse.json({ id: Number(params.id), name: 'Milo' }))] };
