export type Image = { id: string; url: string; sourceUrl: string; creator: string; license: string; licenseUrl: string; attribution: string };
export type Plant = { taxonId: string; name: string; scientificName: string; image: Image };
export type Question = { id: string; prompt: string; image: Image; options: string[] };
export type Unit = { id: string; title: string; plants: Plant[]; questions: Question[] };
export type Course = { id: string; state: string; stateCode: string; ecoregionCode: string; habitat: string; description: string; available: boolean; reason: string | null; total: number; unitCount: number; catalogVersion: string; units: Unit[] };
export type Catalog = { ready: boolean; version: string | null; coverage: { nationalGeographyComplete: boolean; states: { code: string; status: string; areas: number; availableCourses: number }[] } | null };

let learner = '';

function learnerId() {
  if (!learner) {
    const saved = localStorage.getItem('edufleur-learner');
    learner = saved && /^[a-f\d]{8}(-[a-f\d]{4}){3}-[a-f\d]{12}$/i.test(saved) ? saved : crypto.randomUUID();
    localStorage.setItem('edufleur-learner', learner);
  }
  return learner;
}

export async function api<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch('/api' + path, { method: body === undefined ? 'GET' : 'POST', signal,
    headers: { 'Content-Type': 'application/json', 'X-Learner-Id': learnerId() },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) throw new Error((await response.text()) || `Request failed (${response.status}).`);
  return response.json();
}

export const states: Record<string, string> = Object.fromEntries('AL:Alabama|AZ:Arizona|AR:Arkansas|CA:California|CO:Colorado|CT:Connecticut|DE:Delaware|FL:Florida|GA:Georgia|ID:Idaho|IL:Illinois|IN:Indiana|IA:Iowa|KS:Kansas|KY:Kentucky|LA:Louisiana|ME:Maine|MD:Maryland|MA:Massachusetts|MI:Michigan|MN:Minnesota|MS:Mississippi|MO:Missouri|MT:Montana|NE:Nebraska|NV:Nevada|NH:New Hampshire|NJ:New Jersey|NM:New Mexico|NY:New York|NC:North Carolina|ND:North Dakota|OH:Ohio|OK:Oklahoma|OR:Oregon|PA:Pennsylvania|RI:Rhode Island|SC:South Carolina|SD:South Dakota|TN:Tennessee|TX:Texas|UT:Utah|VT:Vermont|VA:Virginia|WA:Washington|WV:West Virginia|WI:Wisconsin|WY:Wyoming'.split('|').map(s => s.split(':')));
