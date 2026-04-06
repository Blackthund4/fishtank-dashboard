export function okJson(r) { if (!r.ok) throw new Error(r.status); return r.json() }
