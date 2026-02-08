// Budget page: vertical targets cascade to brands via LY revenue share.

var LY_REV = JSON.parse(document.getElementById("ly-rev-data").textContent);

function formatDollar(n) {
    if (!n) return "\u2014";
    return "$" + Math.round(n).toLocaleString("en-US");
}

// ── Marketing Revenue Budget = Vert Rev / (1 - Cancel Rate) ──

function updateMarketingRev(month) {
    var vertInput = document.querySelector('.vert-rev-input[data-month="' + month + '"]');
    var cancelInput = document.querySelector('.cancel-rate-input[data-month="' + month + '"]');
    var span = document.querySelector('.mktg-rev-display[data-month="' + month + '"]');
    if (!vertInput || !cancelInput || !span) return;

    var vertRev = parseFloat(vertInput.value) || 0;
    var cancelPct = parseFloat(cancelInput.value) || 0;

    if (vertRev > 0 && cancelPct < 100) {
        span.textContent = formatDollar(vertRev / (1 - cancelPct / 100));
    } else {
        span.textContent = "\u2014";
    }
}

// ── Redistribute vert rev to non-overridden brands ──

function redistributeBrands(month) {
    var vertInput = document.querySelector('.vert-rev-input[data-month="' + month + '"]');
    var vertTotal = vertInput ? (parseFloat(vertInput.value) || 0) : 0;
    var monthLY = LY_REV[month] || {};

    // Collect all brand inputs + override flags for this month
    var brandInputs = [];
    var brandFlags = [];
    document.querySelectorAll('.brand-rev-input[data-month="' + month + '"]').forEach(function (inp) {
        brandInputs.push(inp);
        var flag = document.querySelector(
            '.override-flag[data-month="' + month + '"][data-brand="' + inp.dataset.brand + '"]'
        );
        brandFlags.push(flag);
    });

    // Sum overridden values; collect auto-allocated inputs
    var overrideTotal = 0;
    var autoInputs = [];
    var autoIds = [];
    for (var i = 0; i < brandInputs.length; i++) {
        if (brandFlags[i] && brandFlags[i].value === "1") {
            overrideTotal += parseFloat(brandInputs[i].value) || 0;
        } else {
            autoInputs.push(brandInputs[i]);
            autoIds.push(brandInputs[i].dataset.brand);
        }
    }

    var remaining = vertTotal - overrideTotal;

    // LY total for auto brands only
    var totalLY = 0;
    for (var i = 0; i < autoIds.length; i++) {
        totalLY += (monthLY[autoIds[i]] || 0);
    }

    var allocated = 0;
    for (var i = 0; i < autoInputs.length; i++) {
        var val;
        if (i === autoInputs.length - 1) {
            val = Math.round(remaining - allocated);
        } else {
            var brandLY = monthLY[autoIds[i]] || 0;
            var share = totalLY > 0 ? brandLY / totalLY : 1 / autoInputs.length;
            val = Math.round(remaining * share);
            allocated += val;
        }
        autoInputs[i].value = val || "";
    }
}

// ── Gray-out brand cells when no vert rev exists for that month ──

function updateBrandState(month) {
    var vertInput = document.querySelector('.vert-rev-input[data-month="' + month + '"]');
    var hasVert = vertInput && parseFloat(vertInput.value) > 0;

    document.querySelectorAll('.brand-rev-input[data-month="' + month + '"]').forEach(function (inp) {
        if (hasVert) {
            inp.classList.remove("empty-month");
            inp.removeAttribute("readonly");
            if (!inp.value) inp.placeholder = "0";
        } else {
            inp.classList.add("empty-month");
            inp.setAttribute("readonly", "");
            inp.value = "";
            inp.placeholder = "\u2014";
        }
    });
}

// ── Wire up Vertical Revenue inputs ──

document.querySelectorAll(".vert-rev-input").forEach(function (input) {
    input.addEventListener("input", function () {
        var month = this.dataset.month;
        redistributeBrands(month);
        updateMarketingRev(month);
        updateBrandState(month);
    });
});

// ── Wire up Cancel Rate inputs ──

document.querySelectorAll(".cancel-rate-input").forEach(function (input) {
    input.addEventListener("input", function () {
        updateMarketingRev(this.dataset.month);
    });
});

// ── Manual brand edit → pin override + redistribute remaining ──

document.querySelectorAll(".brand-rev-input").forEach(function (input) {
    input.addEventListener("input", function () {
        var month = this.dataset.month;
        var brand = this.dataset.brand;
        var flag = document.querySelector(
            '.override-flag[data-month="' + month + '"][data-brand="' + brand + '"]'
        );
        if (flag) flag.value = "1";
        this.classList.add("overridden");
        // Redistribute remaining vert rev among non-overridden brands
        redistributeBrands(month);
    });
});

// ── Initialize brand state on page load ──

document.querySelectorAll(".vert-rev-input").forEach(function (input) {
    updateBrandState(input.dataset.month);
});

// ── Brand search filter ──

var searchBox = document.getElementById("brand-search");
if (searchBox) {
    searchBox.addEventListener("input", function () {
        var q = this.value.toLowerCase();
        document.querySelectorAll(".brand-row").forEach(function (row) {
            var name = row.getAttribute("data-brand-name") || "";
            row.style.display = name.indexOf(q) !== -1 ? "" : "none";
        });
    });
}
