// Personal API token management — wraps the BACKEND-B endpoints at /api/tokens.
//
// Note: POST /api/tokens is the ONLY call that returns the plaintext `token`
// field. The caller must render it to the user immediately and then discard.
// GET /api/tokens does not (and must not) return it.

import { http } from './client';
import type {
  ApiToken,
  GenericSuccess,
  TokenCreateRequest,
  TokenCreateResponse,
} from '../types';

export async function listTokens(): Promise<ApiToken[]> {
  const { data } = await http.get<ApiToken[]>('/tokens');
  return data;
}

export async function createToken(body: TokenCreateRequest): Promise<TokenCreateResponse> {
  const { data } = await http.post<TokenCreateResponse>('/tokens', body);
  return data;
}

export async function revokeToken(id: number): Promise<GenericSuccess> {
  const { data } = await http.delete<GenericSuccess>(`/tokens/${id}`);
  return data;
}
