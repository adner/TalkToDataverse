type MarkdownNode = {
  type: string;
  value?: string;
  url?: string;
  children?: MarkdownNode[];
};

const QUOTED_FILE_PATH = /(["'])((?:[A-Za-z]:\\(?:[^\\/:*?"<>|\r\n]+\\)*[^\\/:*?"<>|\r\n]+|(?:\.?[\\/])?exports[\\/][^"'\r\n]+))\1/gi;
const BARE_FILE_PATH = /(?:[A-Za-z]:\\(?:[^\s\\/:*?"<>|\r\n]+\\)*[^\s\\/:*?"<>|\r\n]+|(?:\.?[\\/])?exports[\\/][^\s"'`<>]+)/gi;
const TRAILING_PROSE_PUNCTUATION = /[),.;:!?\]}]+$/;
const EXACT_WINDOWS_PATH = /^[A-Za-z]:\\(?:[^\\/:*?"<>|\r\n]+\\)*[^\\/:*?"<>|\r\n]+$/;
const EXACT_EXPORT_PATH = /^(?:\.?[\\/])?exports[\\/][^<>:"|?*\r\n]+$/i;

function normalizeWorkspacePath(path: string) {
  return path
    .replace(/^\.[\\/]/, "")
    .replace(/^[\\/](?=exports[\\/])/i, "");
}

export function fileHref(path: string, backendUrl: string) {
  return `${backendUrl}/api/files/open?path=${encodeURIComponent(normalizeWorkspacePath(path))}`;
}

function linkNode(path: string, backendUrl: string, child?: MarkdownNode): MarkdownNode {
  return {
    type: "link",
    url: fileHref(path, backendUrl),
    children: [child ?? { type: "text", value: path }],
  };
}

function splitBarePathMatch(value: string, backendUrl: string): MarkdownNode[] {
  const path = value.replace(TRAILING_PROSE_PUNCTUATION, "");
  const suffix = value.slice(path.length);
  return [
    linkNode(path, backendUrl),
    ...(suffix ? [{ type: "text", value: suffix }] : []),
  ];
}

function linkQuotedPaths(
  value: string,
  backendUrl: string,
  pattern: RegExp,
  fallback: (value: string, backendUrl: string) => MarkdownNode[],
): MarkdownNode[] {
  const nodes: MarkdownNode[] = [];
  let quotedCursor = 0;

  for (const match of value.matchAll(pattern)) {
    const index = match.index;
    if (index > quotedCursor) {
      nodes.push(...fallback(value.slice(quotedCursor, index), backendUrl));
    }
    nodes.push({ type: "text", value: match[1] });
    nodes.push(linkNode(match[2], backendUrl));
    nodes.push({ type: "text", value: match[1] });
    quotedCursor = index + match[0].length;
  }

  nodes.push(...fallback(value.slice(quotedCursor), backendUrl));
  return nodes;
}

function linkBareMatches(value: string, backendUrl: string, pattern: RegExp): MarkdownNode[] {
  const nodes: MarkdownNode[] = [];
  let cursor = 0;

  for (const match of value.matchAll(pattern)) {
    const index = match.index;
    if (index > cursor) nodes.push({ type: "text", value: value.slice(cursor, index) });
    nodes.push(...splitBarePathMatch(match[0], backendUrl));
    cursor = index + match[0].length;
  }

  if (cursor < value.length) nodes.push({ type: "text", value: value.slice(cursor) });
  return nodes;
}

function linkBareText(value: string, backendUrl: string): MarkdownNode[] {
  return linkBareMatches(value, backendUrl, BARE_FILE_PATH);
}

function linkText(value: string, backendUrl: string): MarkdownNode[] {
  return linkQuotedPaths(
    value,
    backendUrl,
    QUOTED_FILE_PATH,
    linkBareText,
  );
}

function mergeAdjacentText(nodes: MarkdownNode[]): MarkdownNode[] {
  const merged: MarkdownNode[] = [];
  for (const node of nodes) {
    const previous = merged.at(-1);
    if (node.type === "text" && previous?.type === "text") {
      previous.value = `${previous.value ?? ""}${node.value ?? ""}`;
    } else {
      merged.push(node);
    }
  }
  return merged;
}

function transformChildren(node: MarkdownNode, backendUrl: string) {
  if (node.type === "link") {
    if (node.url && EXACT_EXPORT_PATH.test(node.url)) {
      node.url = fileHref(node.url, backendUrl);
    }
    return;
  }
  if (!node.children || node.type === "linkReference") return;

  node.children = mergeAdjacentText(node.children.flatMap((child) => {
    if (child.type === "text" && child.value) return linkText(child.value, backendUrl);
    if (
      child.type === "inlineCode"
      && child.value
      && (EXACT_WINDOWS_PATH.test(child.value) || EXACT_EXPORT_PATH.test(child.value))
    ) {
      return [linkNode(child.value, backendUrl, child)];
    }
    transformChildren(child, backendUrl);
    return [child];
  }));
}

export function createWorkspaceFileLinksPlugin(backendUrl: string) {
  return function workspaceFileLinks() {
    return (tree: MarkdownNode) => transformChildren(tree, backendUrl.replace(/\/$/, ""));
  };
}
