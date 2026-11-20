const queryInput = document.getElementById("queryInput");
const searchButton = document.getElementById("searchButton");
const browseRootButton = document.getElementById("browseRootButton");
const expandAllButton = document.getElementById("expandAllButton");
const collapseAllButton = document.getElementById("collapseAllButton");
const summaryEl = document.getElementById("summary");
const treeContainer = document.getElementById("treeContainer");
let currentQuery = "";
let currentMode = "auto";
let currentFields = "chinese,english,code";
let currentSearchController = null;
let clientIndexPromise;

function loadClientIndex() {
  if (!clientIndexPromise) {
    clientIndexPromise = Promise.all([
      import("./index-core.mjs"),
      fetch("/data/dataset.json").then((response) => {
        if (!response.ok) throw new Error(`Dataset request failed: HTTP ${response.status}`);
        return response.json();
      }),
    ]).catch((error) => {
      clientIndexPromise = undefined;
      throw error;
    });
  }
  return clientIndexPromise;
}

async function fetchNavigation(url, options = {}) {
  const requestUrl = new URL(url, location.href);
  const [core, dataset] = await loadClientIndex();
  const handlers = {
    "/search": core.handleSearch,
    "/locate": core.handleLocate,
    "/children": core.handleChildren,
  };
  return handlers[requestUrl.pathname](new Request(requestUrl, options), dataset);
}

let tabularDataPromise;

function loadTabularData() {
  if (!tabularDataPromise) {
    tabularDataPromise = fetch("/data/tabular.json")
      .then((response) => {
        if (!response.ok) throw new Error(`Tabular data request failed: HTTP ${response.status}`);
        return response.json();
      })
      .catch((error) => {
        tabularDataPromise = undefined;
        throw error;
      });
  }
  return tabularDataPromise;
}

function buildSearchUrl(query, mode = "auto") {
  const params = new URLSearchParams();
  params.set("q", query);
  params.set("mode", mode);
  params.set("fields", currentFields);
  return `/search?${params.toString()}`;
}

function sanitize(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function extractNodeReferences(node) {
  const chineseReferences = [];
  const englishReferences = [];
  const chinesePattern = /(?:[-（(]?\s*)(另见|见)\s*([^；;\n]+)/gi;
  const englishPattern = /\b(see also|see)\s+([^;\n]+)/gi;

  for (const match of String(node.chinese || "").matchAll(chinesePattern)) {
    const target = match[2].trim();
    if (target) chineseReferences.push({ kind: match[1], target });
  }
  for (const match of String(node.english || "").matchAll(englishPattern)) {
    const target = match[2].trim();
    if (target) englishReferences.push({ kind: match[1].toLowerCase(), target });
  }

  // The bilingual columns describe the same reference. Always locate by the
  // English target, while retaining the Chinese target for display.
  return [
    ...chineseReferences.map((ref, index) => ({
      ...ref,
      queryTarget: englishReferences[index]?.target || ref.target,
    })),
    ...englishReferences.map((ref) => ({ ...ref, queryTarget: ref.target })),
  ];
}

function navigationUrl(params) {
  const url = new URL(location.href);
  for (const key of ["q", "mode", "locate"]) url.searchParams.delete(key);
  for (const [key, value] of Object.entries(params)) {
    if (value) url.searchParams.set(key, value);
  }
  return url;
}

function pushNavigation(params) {
  const url = navigationUrl(params);
  if (url.href !== location.href) history.pushState(null, "", url);
}

function createRefAnchor(target, queryTarget = target) {
  const a = document.createElement("a");
  a.href = navigationUrl({ locate: queryTarget }).href;
  a.className = "ref-inline";
  a.textContent = target;
  a.addEventListener("click", (event) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (!target) return;
    if (target.includes("亚目") || target.toLowerCase().includes("subcategory")) {
      summaryEl.textContent = "此引用指向子目（亚目），已忽略。";
      return;
    }
    performLocate(queryTarget);
  });
  return a;
}

async function performLocate(target, { updateUrl = true } = {}) {
  await loadNavigation(
    `/locate?target=${encodeURIComponent(target)}&mark=false`,
    "定位失败，请重试。",
    (data) => {
      if (data.ignored) {
        summaryEl.textContent = "此引用指向子目（亚目），已忽略。";
        return;
      }
      currentQuery = "";
      currentMode = "auto";
      setSearchMode("auto");
      queryInput.value = target;
      renderSummary(data);
      renderTree(data);
      if (updateUrl) pushNavigation({ locate: target });
    },
  );
}

function createCodeAnchor(code) {
  const link = document.createElement("a");
  link.href = "#";
  link.className = "code-link";
  link.textContent = code;
  link.addEventListener("click", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    const pdfTab = window.open("", "_blank");
    if (pdfTab) pdfTab.opener = null;

    try {
      const [[core], tabularData] = await Promise.all([loadClientIndex(), loadTabularData()]);
      const { page, pdf_url: pdfPath } = core.lookupTabular(tabularData, code);
      if (!page || !pdfPath) {
        pdfTab?.close();
        summaryEl.textContent = `未找到代码 ${code} 对应的类目表页面。`;
        return;
      }
      const pdfUrl = new URL(pdfPath, location.href);
      pdfUrl.hash = `page=${page}`;
      if (pdfTab && !pdfTab.closed) pdfTab.location.replace(pdfUrl.href);
      else if (!pdfTab) location.assign(pdfUrl.href);
    } catch (error) {
      pdfTab?.close();
      console.error("查询类目表失败：", error);
      summaryEl.textContent = `查询代码 ${code} 的类目表失败，请重试。`;
    }
  });
  return link;
}

function embedRefTargetsInTitle(titleText, rawTitle, refs) {
  const rawLower = rawTitle.toLowerCase();
  const matches = [];
  refs.forEach((ref) => {
    const target = (ref.target || '').trim();
    if (!target) return;
    const targetLower = target.toLowerCase();
    const candidateIndexes = [];
    let searchStart = 0;
    while (true) {
      const index = rawLower.indexOf(targetLower, searchStart);
      if (index === -1) break;
      candidateIndexes.push(index);
      searchStart = index + targetLower.length;
    }
    const markerPattern = /(?:另见|见|see\s+also|see)\s*$/i;
    const index = candidateIndexes.find((candidate) => markerPattern.test(rawTitle.slice(0, candidate)))
      ?? candidateIndexes[0];
    if (index !== -1) {
      matches.push({start: index, end: index + target.length, target, queryTarget: ref.queryTarget || target});
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
    titleText.appendChild(createRefAnchor(match.target, match.queryTarget));
    cursor = match.end;
  });
  if (cursor < rawTitle.length) {
    titleText.appendChild(document.createTextNode(rawTitle.substring(cursor)));
  }
  return true;
}

function appendInlineCodeToTitle(titleText, code) {
  if (!code) return;
  if (titleText.textContent && titleText.textContent.trim()) {
    titleText.appendChild(document.createTextNode(" / "));
  }
  titleText.appendChild(createCodeAnchor(String(code)));
}

// Titles will be clickable when the node/item has references; separate reference-chip UI removed.

const feedbackGeneralButton = document.getElementById("feedbackGeneralButton");
const feedbackDialog = document.getElementById("feedbackDialog");
const feedbackForm = document.getElementById("feedbackForm");
const feedbackRecordName = document.getElementById("feedbackRecordName");
const feedbackType = document.getElementById("feedbackType");
const feedbackProposedValue = document.getElementById("feedbackProposedValue");
const feedbackMessage = document.getElementById("feedbackMessage");
const feedbackContact = document.getElementById("feedbackContact");
const feedbackStatus = document.getElementById("feedbackStatus");
const feedbackSubmitButton = document.getElementById("feedbackSubmitButton");
const feedbackCloseButton = document.getElementById("feedbackCloseButton");
const feedbackCancelButton = document.getElementById("feedbackCancelButton");
let feedbackRecord = {};
let feedbackCloseTimer = null;

function clearFeedbackCloseTimer() {
  if (feedbackCloseTimer !== null) {
    window.clearTimeout(feedbackCloseTimer);
    feedbackCloseTimer = null;
  }
}

function closeFeedback() {
  clearFeedbackCloseTimer();
  feedbackDialog.close();
}

const INDEX_PDF_PAGE_OFFSET = 18;

function formatIndexPage(pdfPage) {
  const printedPage = Number(pdfPage) - INDEX_PDF_PAGE_OFFSET;
  return `页${printedPage}`;
}

function openFeedback(node = null) {
  clearFeedbackCloseTimer();
  feedbackRecord = node
    ? { id: node.id, page: node.page, printed_page: Number(node.page) - INDEX_PDF_PAGE_OFFSET, level: node.level, chinese: node.chinese,
      english: node.english, code: node.code, source_file: node.source_file }
    : {};
  const titleParts = node
    ? [formatIndexPage(node.page), node.chinese, node.english, node.code]
    : ["全局反馈（可用于补充缺少的词条）"];
  feedbackRecordName.textContent = titleParts.filter(Boolean).join(" / ");
  feedbackType.value = node ? "" : "缺少词条";
  feedbackProposedValue.value = "";
  feedbackMessage.value = "";
  feedbackContact.value = "";
  feedbackStatus.textContent = "";
  feedbackSubmitButton.disabled = false;
  feedbackSubmitButton.textContent = "提交反馈";
  feedbackDialog.showModal();
}

async function submitFeedback(event) {
  event.preventDefault();
  clearFeedbackCloseTimer();
  feedbackSubmitButton.disabled = true;
  feedbackSubmitButton.textContent = "正在提交……";
  feedbackStatus.textContent = "正在提交……";
  let submitted = false;
  try {
    const response = await fetch("/api/feedback", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        feedbackType: feedbackType.value,
        proposedValue: feedbackProposedValue.value,
        message: feedbackMessage.value,
        contact: feedbackContact.value,
        url: window.location.href,
        record: feedbackRecord,
      }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) throw new Error(result.error || "提交失败");
    submitted = true;
    feedbackStatus.textContent = `反馈已提交${result.id ? `，编号 ${result.id}` : ""}`;
    feedbackCloseTimer = window.setTimeout(() => {
      feedbackCloseTimer = null;
      feedbackDialog.close();
    }, 1000);
  } catch (error) {
    feedbackStatus.textContent = error instanceof Error ? error.message : "提交失败，请稍后重试。";
  } finally {
    if (!submitted) {
      feedbackSubmitButton.disabled = false;
      feedbackSubmitButton.textContent = "提交反馈";
    }
  }
}

feedbackGeneralButton.addEventListener("click", () => openFeedback(null));
feedbackCloseButton.addEventListener("click", closeFeedback);
feedbackCancelButton.addEventListener("click", closeFeedback);
feedbackForm.addEventListener("submit", submitFeedback);
feedbackDialog.addEventListener("click", (event) => {
  if (event.target === feedbackDialog) closeFeedback();
});


function renderNode(node, asPath = false) {
  // Capture the identity at render time. The tree is lazily expanded and its
  // DOM containers are replaced asynchronously; the request must always use
  // the identity of this rendered node.
  const nodeId = String(node.id || '');
  const wrapper = document.createElement("div");
  wrapper.className = node.matched ? "tree-node matched-node" : "tree-node";
  if (node.has_children || (node.children && node.children.length)) {
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
  titleText.textContent = parts.length ? parts.join(" / ") : "(无标题)";
  heading.appendChild(titleText);

  const meta = document.createElement("button");
  meta.type = "button";
  meta.className = "node-meta";
  const levelLabel = document.createElement("span");
  levelLabel.textContent = `层级 ${node.level}`;
  const pageLabel = document.createElement("span");
  pageLabel.textContent = formatIndexPage(node.page);
  meta.append(levelLabel, pageLabel);
  meta.title = "反馈此词条";
  meta.setAttribute("aria-label", `${levelLabel.textContent}，${pageLabel.textContent}，反馈此词条`);
  meta.addEventListener("click", (event) => {
    event.stopPropagation();
    openFeedback(node);
  });

  title.appendChild(heading);
  title.appendChild(meta);
  wrapper.appendChild(title);

  const details = document.createElement("div");
  details.className = "node-details";
  details.innerHTML = "";

  // Rebuild from both bilingual columns so Chinese references can use their
  // corresponding English target for locating.
  const nodeReferences = extractNodeReferences(node);
  if (nodeReferences.length) {
    const refs = nodeReferences.filter((r) => {
      const kl = (r.kind || '').toLowerCase();
      const tgt = (r.target || '').trim();
      return tgt && (kl.includes('见') || kl.includes('see')) && !(tgt.includes('亚目') || tgt.toLowerCase().includes('subcategory'));
    });
    if (refs.length) {
      const rawTitle = [node.chinese, node.english].filter(Boolean).join(' / ');
      const embedded = embedRefTargetsInTitle(titleText, rawTitle, refs);
      if (!embedded) {
        const firstRef = refs[0];
        const a = createRefAnchor((firstRef.target || '').trim(), (firstRef.queryTarget || firstRef.target || '').trim());
        titleText.appendChild(a);
      }
    }
  }

  appendInlineCodeToTitle(titleText, node.code);

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
        const url = `/children?id=${encodeURIComponent(nodeId)}&q=${encodeURIComponent(currentQuery)}&mode=${encodeURIComponent(currentMode)}&fields=${encodeURIComponent(currentFields)}`;
        const data = await fetchNavigation(url);
        fullContainer = document.createElement("div");
        fullContainer.className = "child-list";
        if (data.children && data.children.length) {
          data.children.forEach((child) => fullContainer.appendChild(renderNode(child, false)));
        }
        loaded = true;
      } catch (error) {
        console.error("加载子节点失败：", error);
      }
    };

    const updateIcon = () => {
      const expanded = !wrapper.classList.contains("collapsed");
      toggleIcon.style.transform = expanded ? "rotate(0deg)" : "rotate(-90deg)";
      title.setAttribute("aria-expanded", String(expanded));
    };

    const toggleNode = async () => {
      const isCollapsed = wrapper.classList.contains("collapsed");
      if (isCollapsed) {
        if (!node.has_children && pathContainer) {
          wrapper.classList.remove("collapsed");
          updateIcon();
          return;
        }
        if (node.has_children && !loaded) {
          await loadChildren();
          if (!loaded) return;
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

    wrapper.__toggleNode = toggleNode;
    wrapper.__expandNode = async () => {
      if (wrapper.classList.contains("collapsed")) {
        await toggleNode();
      }
    };
    wrapper.__collapseNode = async () => {
      if (!wrapper.classList.contains("collapsed")) {
        await toggleNode();
      }
    };

    title.tabIndex = 0;
    title.addEventListener("click", (event) => {
      if (!event.target.closest("a, button")) toggleNode();
    });
    title.addEventListener("keydown", (event) => {
      if (event.target !== title || event.repeat || (event.key !== "Enter" && event.key !== " ")) return;
      event.preventDefault();
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

function renderTree(data) {
  treeContainer.innerHTML = "";
  if (!data.tree || !data.tree.length) {
    treeContainer.innerHTML = "<p>暂无分级索引结果。</p>";
    return;
  }
  const fragment = document.createDocumentFragment();
  data.tree.forEach((node) => fragment.appendChild(renderNode(node, true)));
  treeContainer.appendChild(fragment);
}

function setSearchMode(mode) {
  const radios = Array.from(document.querySelectorAll("input[name='searchMode']"));
  (radios.find((radio) => radio.value === mode) || radios.find((radio) => radio.value === "auto")).checked = true;
}

async function loadNavigation(url, errorMessage, render) {
  currentSearchController?.abort();
  const controller = new AbortController();
  currentSearchController = controller;

  try {
    const data = await fetchNavigation(url, { signal: controller.signal });
    if (!controller.signal.aborted) render(data);
  } catch (error) {
    if (controller.signal.aborted) return;
    summaryEl.textContent = errorMessage;
    console.error(error);
  } finally {
    if (currentSearchController === controller) currentSearchController = null;
  }
}

async function performSearch({ updateUrl = true } = {}) {
  const query = queryInput.value.trim();
  const mode = document.querySelector("input[name='searchMode']:checked").value;
  summaryEl.textContent = "加载中...";
  treeContainer.replaceChildren();

  await loadNavigation(buildSearchUrl(query, mode), "检索失败，请稍后重试。", (data) => {
    currentQuery = query;
    currentMode = mode;
    renderSummary(data);
    renderTree(data);
    if (updateUrl) pushNavigation({ q: query, mode: mode === "auto" ? "" : mode });
  });
}

function restoreNavigationState() {
  const params = new URL(location.href).searchParams;
  const target = params.get("locate");
  if (target) return performLocate(target, { updateUrl: false });

  queryInput.value = params.get("q") || "";
  setSearchMode(params.get("mode") || "auto");
  return performSearch({ updateUrl: false });
}

searchButton.addEventListener("click", () => performSearch());
browseRootButton.addEventListener("click", () => {
  queryInput.value = "";
  performSearch();
});
expandAllButton.addEventListener("click", async () => {
  await toggleAllNodes(false);
});
collapseAllButton.addEventListener("click", async () => {
  await toggleAllNodes(true);
});
queryInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    performSearch();
  }
});

window.addEventListener("load", restoreNavigationState);
window.addEventListener("popstate", restoreNavigationState);

async function toggleAllNodes(collapse) {
  if (collapse) {
    const expandedNodes = Array.from(treeContainer.querySelectorAll(".tree-node.has-children")).filter(
      (node) => !node.classList.contains("collapsed")
    );
    for (const node of expandedNodes.reverse()) {
      if (typeof node.__collapseNode === "function") {
        await node.__collapseNode();
      } else {
        node.classList.add("collapsed");
      }
    }
    return;
  }

  // Expand recursively so lazily loaded descendants are included.
  const maxPasses = 64;
  for (let pass = 0; pass < maxPasses; pass += 1) {
    const collapsedNodes = Array.from(treeContainer.querySelectorAll(".tree-node.has-children.collapsed"));
    if (!collapsedNodes.length) {
      break;
    }

    const frontier = collapsedNodes.filter((node) => !node.parentElement?.closest(".tree-node.has-children.collapsed"));
    const targets = frontier.length ? frontier : collapsedNodes;
    for (const node of targets) {
      if (typeof node.__expandNode === "function") {
        await node.__expandNode();
      } else {
        node.classList.remove("collapsed");
      }
    }
  }
}
