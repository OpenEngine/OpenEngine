/** Import a generated JSON manifest without trusting JSON.parse's implicit any. */
export function parseManifest(source: string): string[] {
  let value: unknown;
  try {
    value = JSON.parse(source);
  } catch {
    throw new Error(`Invalid diagram manifest: expected JSON`);
  }
  const allowed = /^docs\/architecture\/schema\.(mermaid|png)$/;
  if (!Array.isArray(value) || !value.every(
    (entry: unknown): entry is string => typeof entry === 'string' && allowed.test(entry),
  )) {
    throw new Error(`Invalid diagram manifest: expected an array of generated schema paths`);
  }
  return [...new Set(value)];
}
