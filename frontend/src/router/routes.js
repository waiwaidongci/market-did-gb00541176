export const pages = ['overview', 'inputs', 'trucks', 'run', 'results', 'review', 'delivery'];

// 与后端 SPEC['pages'] 同序，按页面 key 取中文标题，避免下标硬编码。
export function pageTitle(spec, key) {
  return spec.pages[pages.indexOf(key)];
}

export function route() {
  const [page, rawId, offset] = location.hash.slice(1).split('/');
  const key = pages.includes(page) ? page : 'overview';
  let slug = null;
  try {
    slug = rawId ? decodeURIComponent(rawId) : null;
  } catch {
    slug = rawId || null;
  }
  return {
    page: key,
    id: Number(rawId) || null,
    slug,
    offset: Math.max(0, Number(offset) || 0),
  };
}
