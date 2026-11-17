import { createOpenApiHttp } from 'openapi-msw';
const http = createOpenApiHttp({ baseUrl: 'http://petstore.swagger.io/v1' });
export const handlers = [http.get('/pets/{petId}', ({ params, response }) => response(200).json({ id: params.petId === 'stale' ? '1' : Number(params.petId), name: 'Milo' }))];
