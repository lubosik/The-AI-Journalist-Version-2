(() => {
  const brand = () => {
    if (document.title !== "HERALD Intelligence") {
      document.title = "HERALD Intelligence";
    }
    document.documentElement.style.scrollBehavior = "smooth";

    const placeholder = document.querySelector("textarea");
    if (placeholder && placeholder.placeholder !== "Ask HERALD anything. Type / for commands...") {
      placeholder.placeholder = "Ask HERALD anything. Type / for commands...";
    }

    document.querySelectorAll("a").forEach((link) => {
      const text = (link.textContent || "").trim().toLowerCase();
      if (text.includes("chainlit") && !text.includes("herald") && link.style.display !== "none") {
        link.style.display = "none";
      }
    });

    document.querySelectorAll("h1, h2").forEach((heading) => {
      if ((heading.textContent || "").trim() === "Login to access the app") {
        heading.textContent = "HERALD Intelligence";
        if (!heading.nextElementSibling?.classList.contains("herald-login-subtitle")) {
          const subtitle = document.createElement("p");
          subtitle.className = "herald-login-subtitle";
          subtitle.textContent = "Private intelligence workspace";
          heading.insertAdjacentElement("afterend", subtitle);
        }
      }
    });
  };

  document.addEventListener("DOMContentLoaded", brand);
  new MutationObserver(brand).observe(document.documentElement, {
    childList: true,
    subtree: true,
  });
})();
