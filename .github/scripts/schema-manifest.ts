/** Import a generated JSON manifest without trusting JSON.parse's implicit any. */
export function parseManifest(source: string, kind: 'schema' | 'diagram' | 'packages'): string[] {
  let value: unknown;
  try {
    value = JSON.parse(source);
  } catch {
    throw new Error(`Invalid ${kind} manifest: expected JSON`);
  }
  const allowed = kind === 'schema'
    ? /^(sqlite|sqlite_graph|postgres)\/[A-Za-z0-9_-]+\.md$/
    : kind === 'packages' ? /^docs\/architecture\/packages\.(mermaid|png)$/
    : /^docs\/architecture\/schema\.(mermaid|png)$/;
  if (!Array.isArray(value) || !value.every(
    (entry: unknown): entry is string => typeof entry === 'string' && allowed.test(entry),
  )) {
    throw new Error(`Invalid ${kind} manifest: expected an array of generated schema paths`);
  }
  return [...new Set(value)];
}
