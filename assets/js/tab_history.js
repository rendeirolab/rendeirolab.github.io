(() => {
    "use strict";

    function activateTab(name) {
        var btn = document.getElementById("tab-" + name);
        if (!btn) return;
        document.querySelectorAll("#year-tabs .btn").forEach(function (b) {
            b.className = "btn btn-outline-primary";
        });
        btn.className = "btn btn-primary";
        var url = btn.getAttribute("hx-get");
        var target = btn.getAttribute("hx-target");
        if (url && target && typeof htmx !== "undefined") {
            htmx.ajax("GET", url, { target: target, swap: "innerHTML" }).then(function () {
                var el = document.getElementById(target.replace(/^#/, ""));
                if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
            });
        }
    }

    // Activate tab from URL hash on initial load
    (function () {
        var hash = window.location.hash.slice(1);
        if (hash) activateTab(hash);
    })();

    // Update URL when a tab is clicked
    document.getElementById("year-tabs").addEventListener("click", function (e) {
        var btn = e.target.closest("button[id^=\"tab-\"]");
        if (btn) history.pushState(null, "", "#" + btn.id.replace("tab-", ""));
    });

    // Activate tab on browser back/forward
    window.addEventListener("popstate", function () {
        var hash = window.location.hash.slice(1);
        if (hash) activateTab(hash);
    });
})();
