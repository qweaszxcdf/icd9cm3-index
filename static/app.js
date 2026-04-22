const queryInput = document.getElementById("queryInput");
const searchButton = document.getElementById("searchButton");
const browseRootButton = document.getElementById("browseRootButton");
const expandAllButton = document.getElementById("expandAllButton");
const collapseAllButton = document.getElementById("collapseAllButton");
const summaryEl = document.getElementById("summary");
const reverseContainer = document.getElementById("reverseContainer");
const treeContainer = document.getElementById("treeContainer");
let currentQuery = "";
let currentMode = "auto";
let currentFields = "chinese,english,code";

function buildSearchUrl(query, mode = "auto") {
  const params = new URLSearchParams();
  params.set("q", query);
  params.set("mode", mode);
  params.set("fields", currentFields);
  return `/api/search?${params.toString()}`;
}

function sanitize(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function createRefAnchor(target) {
  const a = document.createElement('a');
  a.href = '#';
  a.className = 'ref-inline';
  a.textContent = target;
  a.addEventListener('click', async (ev) => {
    ev.preventDefault();
    if (!target) return;
    const tl = target.toLowerCase();
    if (target.includes('亚目') || tl.includes('subcategory')) {
      summaryEl.textContent = '此引用指向子目（亚目），已忽略。';
      return;
    }
    try {
      const url = `/api/locate?target=${encodeURIComponent(target)}&mark=false`;
      const resp = await fetch(url);
      const data = await resp.json();
      if (data.ignored) {
        summaryEl.textContent = '此引用指向子目（亚目），已忽略。';
        return;
      }
      renderSummary(data);
      renderReverseHits(data.reverse_hits || []);
      renderTree(data);
    } catch (err) {
      console.error('定位引用失败：', err);
      summaryEl.textContent = '定位失败，请重试。';
    }
  });
  return a;
}

function embedRefTargetsInTitle(titleText, rawTitle, refs) {
  const rawLower = rawTitle.toLowerCase();
  const matches = [];
  refs.forEach((ref) => {
    const target = (ref.target || '').trim();
    if (!target) return;
    const targetLower = target.toLowerCase();
    const index = rawLower.indexOf(targetLower);
    if (index !== -1) {
      matches.push({start: index, end: index + target.length, target});
    }
  });
  if (!matches.length) {
    return false;
  }
  matches.sort((a, b) => a.start - b.start || b.end - a.end);
  const merged = [];
  let lastEnd = -1;
  matches.forEach((match) => {
    if (match.start >= lastEnd) {
      merged.push(match);
      lastEnd = match.end;
    }
  });
  titleText.textContent = '';
  let cursor = 0;
  merged.forEach((match) => {
    if (match.start > cursor) {
      titleText.appendChild(document.createTextNode(rawTitle.substring(cursor, match.start)));
    }
    titleText.appendChild(createRefAnchor(match.target));
    cursor = match.end;
  });
  if (cursor < rawTitle.length) {
    titleText.appendChild(document.createTextNode(rawTitle.substring(cursor)));
  }
  return true;
}

// Titles will be clickable when the node/item has references; separate reference-chip UI removed.

function renderNode(node, asPath = false) {
  const wrapper = document.createElement("div");
  wrapper.className = node.matched ? "tree-node matched-node" : "tree-node";
  if (node.children && node.children.length) {
    wrapper.classList.add("has-children");
  }

  const title = document.createElement("div");
  title.className = "node-label";

  const heading = document.createElement("div");
  heading.className = "node-heading";

  const toggleIcon = document.createElement("span");
  toggleIcon.className = "toggle-icon";
  const hasChildren = node.has_children || (node.children && node.children.length);
  toggleIcon.textContent = hasChildren ? "▾" : "";
  toggleIcon.style.cursor = hasChildren ? "pointer" : "default";
  heading.appendChild(toggleIcon);

  const titleText = document.createElement("div");
  titleText.className = "node-title";
  const parts = [];
  if (node.chinese) parts.push(sanitize(node.chinese));
  if (node.english) parts.push(sanitize(node.english));
  if (node.code) parts.push(sanitize(node.code));
  titleText.textContent = parts.length ? parts.join(" / ") : "(无标题)";
  heading.appendChild(titleText);

  const meta = document.createElement("div");
  meta.className = "node-meta";
  meta.textContent = `层级 ${node.level} · 页 ${node.page}`;

  title.appendChild(heading);
  title.appendChild(meta);
  wrapper.appendChild(title);

  const details = document.createElement("div");
  details.className = "node-details";
  details.innerHTML = "";

  if (node.references && node.references.length) {
    const refs = node.references.filter((r) => {
      const kl = (r.kind || '').toLowerCase();
      const tgt = (r.target || '').trim();
      return tgt && (kl.includes('见') || kl.includes('see')) && !(tgt.includes('亚目') || tgt.toLowerCase().includes('subcategory'));
    });
    if (refs.length) {
      const rawTitle = [node.chinese, node.english, node.code].filter(Boolean).join(' / ');
      const embedded = embedRefTargetsInTitle(titleText, rawTitle, refs);
      if (!embedded) {
        const a = createRefAnchor((refs[0].target || '').trim());
        titleText.appendChild(a);
      }
    }
  }

  if (hasChildren) {
    // If this node is part of the server-provided path, render its path-children
    // visibly outside the collapsible details. Otherwise, do not render the
    // path container so children stay inside the collapsible subtree.
    let pathContainer = null;
    if (asPath) {
      pathContainer = document.createElement("div");
      pathContainer.className = "path-children";
      if (node.children && node.children.length) {
        node.children.forEach((child) => pathContainer.appendChild(renderNode(child, true)));
      }
      wrapper.appendChild(pathContainer);
    }

    title.classList.add("expandable");
    // Parent nodes default to collapsed (showing only the path).
    wrapper.classList.add("collapsed");
    let loaded = false;
    let fullContainer = null;

    const loadChildren = async () => {
      if (!node.has_children) {
        return;
      }
      try {
        const url = `/api/children?id=${encodeURIComponent(node.id)}&q=${encodeURIComponent(currentQuery)}&mode=${encodeURIComponent(currentMode)}&fields=${encodeURIComponent(currentFields)}`;
        const response = await fetch(url);
        const data = await response.json();
        fullContainer = document.createElement("div");
        fullContainer.className = "child-list";
        if (data.children && data.children.length) {
          data.children.forEach((child) => fullContainer.appendChild(renderNode(child, false)));
        }
      } catch (error) {
        console.error("加载子节点失败：", error);
      }
      loaded = true;
    };

    const updateIcon = () => {
      toggleIcon.style.transform = wrapper.classList.contains("collapsed") ? "rotate(-90deg)" : "rotate(0deg)";
    };

    const toggleNode = async () => {
      const isCollapsed = wrapper.classList.contains("collapsed");
      if (isCollapsed) {
        if (node.has_children && !loaded) {
          await loadChildren();
        }
        // Replace the visible path with the full child subtree in the same position
        if (pathContainer && pathContainer.parentNode) {
          pathContainer.parentNode.removeChild(pathContainer);
        }
        if (!fullContainer) {
          fullContainer = document.createElement("div");
          fullContainer.className = "child-list";
        }
        if (!fullContainer.parentNode) {
          wrapper.insertBefore(fullContainer, details);
        }
        wrapper.classList.remove("collapsed");
      } else {
        // Collapse: remove full subtree and restore the original path
        if (fullContainer && fullContainer.parentNode) {
          fullContainer.parentNode.removeChild(fullContainer);
        }
        if (pathContainer && !pathContainer.parentNode) {
          wrapper.insertBefore(pathContainer, details);
        }
        wrapper.classList.add("collapsed");
      }
      updateIcon();
    };

    toggleIcon.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      toggleNode();
    });

    updateIcon();
  }

  wrapper.appendChild(details);
  return wrapper;
}

function renderSummary(data) {
  summaryEl.innerHTML = `检索到 <strong>${data.count}</strong> 条结果`;
}

function renderReverseHits(rows) {
  reverseContainer.innerHTML = "";
  if (!rows || !rows.length) {
    reverseContainer.style.display = "none";
    return;
  }
  reverseContainer.style.display = "block";
  reverseContainer.innerHTML = `<strong>反向索引（引用该代码的条目）：</strong> ${rows.length} 条`;
  rows.forEach((row) => {
    const item = document.createElement("div");
    item.className = "list-item";

    const title = document.createElement("div");
    title.className = "item-label";
    title.innerHTML = `<div class="item-title">${sanitize(row.chinese || row.english || "(无标题)")}</div>`;

    const meta = document.createElement("div");
    meta.className = "item-meta";
    meta.textContent = `代码: ${sanitize(row.code || "-")} · 层级 ${row.level} · 页 ${row.page}`;
    title.appendChild(meta);
    item.appendChild(title);

    const details = document.createElement("div");
    details.className = "item-details";
    details.innerHTML = `<div>${sanitize(row.chinese)}</div><div>${sanitize(row.english)}</div>`;
    // Do not render reference texts in item details. Instead, embed all suitable
    // '见'/'see' reference targets found in the item title.
    if (row.references && row.references.length) {
      const refs = row.references.filter((r) => {
        const kl = (r.kind || '').toLowerCase();
        const tgt = (r.target || '').trim();
        return tgt && (kl.includes('见') || kl.includes('see')) && !(tgt.includes('亚目') || tgt.toLowerCase().includes('subcategory'));
      });
      if (refs.length) {
        const rawTitle = (row.chinese || row.english || '(无标题)');
        const itemTitleEl = title.querySelector('.item-title');
        if (itemTitleEl) {
          const embedded = embedRefTargetsInTitle(itemTitleEl, rawTitle, refs);
          if (!embedded) {
            const a = createRefAnchor((refs[0].target || '').trim());
            itemTitleEl.appendChild(a);
          }
        }
      }
    }
    item.appendChild(details);
    reverseContainer.appendChild(item);
  });
}

function renderTree(data) {
  treeContainer.innerHTML = "";
  if (!data.tree || !data.tree.length) {
    treeContainer.innerHTML = "<p>暂无分级索引结果。</p>";
    return;
  }
  data.tree.forEach((node) => treeContainer.appendChild(renderNode(node, true)));
}

async function performSearch() {
  const query = queryInput.value.trim();
  currentQuery = query;
  currentMode = document.querySelector("input[name='searchMode']:checked").value;
  const url = buildSearchUrl(query, currentMode);
  summaryEl.textContent = "加载中...";
  reverseContainer.innerHTML = "";
  treeContainer.innerHTML = "";

  try {
    const response = await fetch(url);
    const data = await response.json();
    renderSummary(data);
    renderReverseHits(data.reverse_hits || []);
    renderTree(data);
  } catch (error) {
    summaryEl.textContent = "检索失败，请稍后重试。";
    console.error(error);
  }
}

searchButton.addEventListener("click", () => performSearch());
browseRootButton.addEventListener("click", () => {
  queryInput.value = "";
  performSearch();
});
expandAllButton.addEventListener("click", () => toggleAllNodes(false));
collapseAllButton.addEventListener("click", () => toggleAllNodes(true));
queryInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    performSearch();
  }
});

window.addEventListener("load", () => {
  performSearch();
});

function toggleAllNodes(collapse) {
  const nodes = treeContainer.querySelectorAll(".tree-node.has-children");
  nodes.forEach((node) => {
    if (collapse) {
      node.classList.add("collapsed");
    } else {
      node.classList.remove("collapsed");
    }
  });
}
