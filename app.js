if (window.innerWidth > 768) document.body.classList.remove("sb-closed");
            // --- Auth state ---
            let AUTH_STATE = { enabled: false, user: null, needs_setup: false };

            async function checkAuth() {
                try {
                    const r = await fetch("/api/auth/me");
                    const d = await r.json();
                    AUTH_STATE.enabled = !!d.auth_enabled;
                    AUTH_STATE.user = d.user || null;
                    AUTH_STATE.needs_setup = !!d.needs_setup;
                } catch (e) {
                    AUTH_STATE.enabled = false;
                    AUTH_STATE.user = null;
                }
                if (!AUTH_STATE.enabled) {
                    // Auth disabled — hide auth screen, proceed
                    document
                        .getElementById("auth-screen")
                        .classList.remove("open");
                    return true;
                }
                if (AUTH_STATE.needs_setup) {
                    showAuthScreen(true);
                    return false;
                }
                if (!AUTH_STATE.user) {
                    showAuthScreen(false);
                    return false;
                }
                // Logged in
                document.getElementById("auth-screen").classList.remove("open");
                showUserAvatar();
                return true;
            }

            function showAuthScreen(isSetup) {
                const s = document.getElementById("auth-screen");
                s.classList.add("open");
                document.getElementById("auth-title").textContent =
                    isSetup ? "Create Admin Account" : "Sign In";
                document.getElementById("auth-sub").textContent =
                    isSetup ?
                        "Set up your admin account to get started"
                    :   "Sign in to continue";
                const nameWrap = document.getElementById("a-name-wrap");
                if (isSetup) nameWrap.classList.remove("hidden");
                else nameWrap.classList.add("hidden");
                const pass2Wrap = document.getElementById("a-pass2-wrap");
                if (isSetup) pass2Wrap.classList.remove("hidden");
                else pass2Wrap.classList.add("hidden");
                document.getElementById("auth-btn").textContent =
                    isSetup ? "Create Account" : "Sign In";
                document.getElementById("auth-err").style.display = "none";
                // Reset auth form to initial state (clear any TOTP step)
                document.getElementById("a-totp-wrap").classList.add("hidden");
                document.getElementById("a-user").parentElement.style.display =
                    "";
                document.getElementById("a-pass").parentElement.style.display =
                    "";
                const btn = document.getElementById("auth-btn");
                btn.onclick = function () {
                    doAuth();
                };
                btn.disabled = false;
                document.getElementById("a-user").value = "";
                document.getElementById("a-pass").value = "";
                AUTH_STATE.needs_setup = isSetup;
            }

            async function doAuth() {
                const btn = document.getElementById("auth-btn");
                const errEl = document.getElementById("auth-err");
                errEl.style.display = "none";
                const username = document.getElementById("a-user").value.trim();
                const password = document.getElementById("a-pass").value;

                if (!username || !password) {
                    errEl.textContent = "Please fill in all fields";
                    errEl.style.display = "block";
                    return;
                }

                if (AUTH_STATE.needs_setup) {
                    const pass2 = document.getElementById("a-pass2").value;
                    if (password !== pass2) {
                        errEl.textContent = "Passwords do not match";
                        errEl.style.display = "block";
                        return;
                    }
                    const display_name =
                        document.getElementById("a-name").value.trim() ||
                        username;
                    btn.disabled = true;
                    btn.textContent = "Creating...";
                    try {
                        const r = await fetch("/api/auth/setup", {
                            method: "POST",
                            headers: {
                                "Content-Type": "application/json",
                                "X-Requested-With": "lm-chat",
                            },
                            body: JSON.stringify({
                                username,
                                password,
                                display_name,
                            }),
                        });
                        const d = await r.json();
                        if (!r.ok) {
                            errEl.textContent = d.error || "Setup failed";
                            errEl.style.display = "block";
                            btn.disabled = false;
                            btn.textContent = "Create Account";
                            return;
                        }
                        AUTH_STATE.user = d.user;
                        AUTH_STATE.needs_setup = false;
                        document
                            .getElementById("auth-screen")
                            .classList.remove("open");
                        showUserAvatar();
                        initApp();
                    } catch (e) {
                        errEl.textContent = "Connection failed";
                        errEl.style.display = "block";
                    } finally {
                        btn.disabled = false;
                        btn.textContent = "Create Account";
                    }
                } else {
                    btn.disabled = true;
                    btn.textContent = "Signing in...";
                    try {
                        const r = await fetch("/api/auth/login", {
                            method: "POST",
                            headers: {
                                "Content-Type": "application/json",
                                "X-Requested-With": "lm-chat",
                            },
                            body: JSON.stringify({ username, password }),
                        });
                        const d = await r.json();
                        if (!r.ok) {
                            errEl.textContent = d.error || "Login failed";
                            errEl.style.display = "block";
                            btn.disabled = false;
                            btn.textContent = "Sign In";
                            return;
                        }
                        if (d.needs_totp) {
                            // Show TOTP input, hide username/password
                            document.getElementById(
                                "a-totp-wrap",
                            ).classList.remove("hidden");
                            document.getElementById(
                                "a-user",
                            ).parentElement.style.display = "none";
                            document.getElementById(
                                "a-pass",
                            ).parentElement.style.display = "none";
                            document.getElementById("auth-title").textContent =
                                "Enter 2FA Code";
                            document.getElementById("auth-sub").textContent =
                                "Enter the code from your authenticator app";
                            btn.textContent = "Verify";
                            btn.onclick = async function (e) {
                                e.preventDefault();
                                const code = document
                                    .getElementById("a-totp")
                                    .value.trim();
                                errEl.style.display = "none";
                                if (code.length !== 6) {
                                    errEl.textContent = "Enter a 6-digit code";
                                    errEl.style.display = "block";
                                    return;
                                }
                                btn.disabled = true;
                                btn.textContent = "Verifying...";
                                try {
                                    const r2 = await fetch(
                                        "/api/auth/totp/login",
                                        {
                                            method: "POST",
                                            headers: {
                                                "Content-Type":
                                                    "application/json",
                                                "X-Requested-With": "lm-chat",
                                            },
                                            body: JSON.stringify({
                                                partial_token: d.partial_token,
                                                code,
                                            }),
                                        },
                                    );
                                    const d2 = await r2.json();
                                    if (!r2.ok) {
                                        errEl.textContent =
                                            d2.error || "Invalid code";
                                        errEl.style.display = "block";
                                        btn.disabled = false;
                                        btn.textContent = "Verify";
                                        return;
                                    }
                                    AUTH_STATE.user = d2.user;
                                    document
                                        .getElementById("auth-screen")
                                        .classList.remove("open");
                                    showUserAvatar();
                                    await initApp();
                                } catch (e2) {
                                    errEl.textContent = "Connection failed";
                                    errEl.style.display = "block";
                                } finally {
                                    btn.disabled = false;
                                    btn.textContent = "Verify";
                                }
                            };
                            document.getElementById("a-totp").focus();
                            return;
                        }
                        AUTH_STATE.user = d.user;
                        document
                            .getElementById("auth-screen")
                            .classList.remove("open");
                        showUserAvatar();
                        initApp();
                    } catch (e) {
                        if (e.message !== "unauthorized") {
                            errEl.textContent = "Connection failed";
                            errEl.style.display = "block";
                        }
                        btn.disabled = false;
                        btn.textContent = "Sign In";
                    }
                }
            }

            async function doLogout() {
                document.getElementById("user-dd").classList.remove("open");
                await apiFetch("/api/auth/logout", { method: "POST" });
                AUTH_STATE.user = null;
                document.getElementById("user-avatar").classList.add("hidden");
                const gear = document.getElementById("global-settings-btn");
                if (gear) gear.classList.remove("hidden");
                showAuthScreen(false);
            }

            function showUserAvatar() {
                if (!AUTH_STATE.enabled || !AUTH_STATE.user) return;
                const av = document.getElementById("user-avatar");
                const name =
                    AUTH_STATE.user.display_name ||
                    AUTH_STATE.user.username ||
                    "?";
                // Save dropdown before wiping
                const dd = document.getElementById("user-dd");
                av.textContent = name.charAt(0).toUpperCase();
                if (dd) av.appendChild(dd);
                av.classList.remove("hidden");
                document.getElementById("user-dd-name").textContent = name;
                // Hide redundant gear — user menu has Settings
                const gear = document.getElementById("global-settings-btn");
                if (gear) gear.classList.add("hidden");
            }

            function toggleUserDD() {
                document.getElementById("user-dd").classList.toggle("open");
            }
            // Close user dropdown on outside click
            document.addEventListener("click", (e) => {
                const av = document.getElementById("user-avatar");
                if (av && !av.contains(e.target))
                    document.getElementById("user-dd").classList.remove("open");
            });

            // --- System Settings Panel ---
            let settingsOpen = false;
            let settingsTab = "chat";

            function openSettings(tab) {
                if (tab) settingsTab = tab;
                settingsOpen = true;
                $("scroll").style.display = "none";
                $("input-area").style.display = "none";
                if ($("starters")) $("starters").style.display = "none";
                if ($("thinking")) $("thinking").style.display = "none";
                $("sys-settings").classList.add("open");
                renderSettingsTab();
            }

            function closeSettings() {
                // Return live DOM nodes to hidden store
                const store = $("chat-settings-store");
                const c = $("chat-settings-content");
                if (c) store.appendChild(c);
                const m = $("memory-settings-content");
                if (m) store.appendChild(m);
                const s = $("starters-settings-content");
                if (s) store.appendChild(s);
                settingsOpen = false;
                $("sys-settings").classList.remove("open");
                $("sys-settings").innerHTML = "";
                $("scroll").style.display = "";
                $("input-area").style.display = "";
                if ($("starters")) $("starters").style.display = "";
            }

            function switchSettingsTab(tab) {
                settingsTab = tab;
                renderSettingsTab();
            }

            function renderSettingsTab() {
                // Return live DOM nodes to hidden store before re-render
                const store = $("chat-settings-store");
                const chatContent = $("chat-settings-content");
                if (chatContent) store.appendChild(chatContent);
                const memContent = $("memory-settings-content");
                if (memContent) store.appendChild(memContent);
                const startersContent = $("starters-settings-content");
                if (startersContent) store.appendChild(startersContent);

                const el = $("sys-settings");
                const isAdmin = AUTH_STATE.user && AUTH_STATE.user.is_admin;
                const userTabHTML =
                    isAdmin ?
                        `<button class="sys-tab${settingsTab === "users" ? " active" : ""}" data-action="switch-tab" data-tab="users">Users</button>`
                    :   "";

                const tabDef = [
                    ["chat", "Chat"],
                    ["memory", "Memory"],
                    ["starters", "Starters"],
                    ["server", "Server"],
                    ["profile", "Profile"],
                    ["security", "Security"],
                ];
                let header = `<div class="sys-header"><button class="sys-back" data-action="close-settings">&larr;</button><h2>Settings</h2></div>`;
                let tabs =
                    '<div class="sys-tabs">' +
                    tabDef
                        .map(
                            ([k, v]) =>
                                `<button class="sys-tab${settingsTab === k ? " active" : ""}" data-action="switch-tab" data-tab="${k}">${v}</button>`,
                        )
                        .join("") +
                    userTabHTML +
                    "</div>";
                let content = '<div class="sys-content" id="sys-content">';

                if (settingsTab === "chat")
                    content += '<div id="chat-tab-slot"></div>';
                else if (settingsTab === "memory")
                    content += '<div id="memory-tab-slot"></div>';
                else if (settingsTab === "starters")
                    content += '<div id="starters-tab-slot"></div>';
                else if (settingsTab === "server") content += renderServerTab();
                else if (settingsTab === "profile")
                    content += renderProfileTab();
                else if (settingsTab === "security")
                    content += renderSecurityTab();
                else if (settingsTab === "users") content += renderUsersTab();

                content += "</div>";
                content += `<div style="text-align:center;padding:var(--sp-7) 0 var(--sp-4);font-size:0.6875rem;color:var(--faint)">LM Chat v${appVersion || "…"}</div>`;
                el.innerHTML = header + tabs + content;

                // Attach settings panel event listeners
                el.querySelector('[data-action="close-settings"]')?.addEventListener('click', closeSettings);
                el.querySelectorAll('[data-action="switch-tab"]').forEach(btn =>
                    btn.addEventListener('click', () => switchSettingsTab(btn.dataset.tab))
                );
                attachSettingsTabListeners(el, settingsTab);

                // Move live DOM nodes into their slots
                if (settingsTab === "chat") {
                    const slot = $("chat-tab-slot");
                    if (slot && chatContent) {
                        slot.appendChild(chatContent);
                        chatContent.style.display = "";
                    }
                } else if (settingsTab === "memory") {
                    const slot = $("memory-tab-slot");
                    if (slot && memContent) {
                        slot.appendChild(memContent);
                        memContent.style.display = "";
                        loadMemoryPanel();
                    }
                } else if (settingsTab === "starters") {
                    const slot = $("starters-tab-slot");
                    if (slot && startersContent) {
                        slot.appendChild(startersContent);
                        startersContent.style.display = "";
                        renderStarterSettings();
                    }
                }
            }

            function attachSettingsTabListeners(el, tab) {
                if (tab === "profile") {
                    el.querySelector('[data-action="save-profile"]')?.addEventListener('click', saveProfile);
                    el.querySelector('[data-action="change-password"]')?.addEventListener('click', doSettingsChangePassword);
                } else if (tab === "security") {
                    el.querySelector('[data-action="disable-totp"]')?.addEventListener('click', disableTotp);
                    el.querySelector('[data-action="start-totp-setup"]')?.addEventListener('click', startTotpSetup);
                } else if (tab === "users") {
                    el.querySelector('[data-action="create-user"]')?.addEventListener('click', doSettingsInvite);
                } else if (tab === "server") {
                    el.querySelector('[data-action="clear-api-key"]')?.addEventListener('click', clearApiKey);
                    el.querySelector('[data-action="save-server-settings"]')?.addEventListener('click', saveServerSettings);
                    el.querySelector('[data-action="add-remote-mcp"]')?.addEventListener('click', addRemoteMcp);
                    el.querySelector('[data-action="toggle-debug"]')?.addEventListener('change', function() { toggleDebugMode(this.checked); });
                }
            }

            function renderProfileTab() {
                const u = AUTH_STATE.user || {};
                return `
    <div class="sys-section">
      <h3>Profile</h3>
      <div class="sys-field"><label>Username</label><input type="text" value="${esc(u.username || "")}" readonly></div>
      <div class="sys-field"><label>Display Name</label><input type="text" id="sp-name" value="${esc(u.display_name || "")}"></div>
      <button class="sys-btn" data-action="save-profile">Save</button>
      <div id="sp-msg"></div>
    </div>
    <div class="sys-section">
      <h3>Change Password</h3>
      <div class="sys-field"><label>Current Password</label><input type="password" id="sp-curpw" autocomplete="current-password"></div>
      <div class="sys-field"><label>New Password</label><input type="password" id="sp-newpw" autocomplete="new-password"></div>
      <div class="sys-field"><label>Confirm New Password</label><input type="password" id="sp-confpw" autocomplete="new-password"></div>
      <button class="sys-btn" data-action="change-password">Change Password</button>
      <div id="sp-pw-msg"></div>
    </div>`;
            }

            async function saveProfile() {
                const name = $("sp-name").value.trim();
                const r = await apiFetch("/api/auth/profile", {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ display_name: name }),
                });
                const d = await r.json();
                const msg = $("sp-msg");
                if (r.ok) {
                    msg.className = "sys-msg ok";
                    msg.textContent = "Saved";
                    AUTH_STATE.user.display_name = name;
                    showUserAvatar();
                } else {
                    msg.className = "sys-msg err";
                    msg.textContent = d.error || "Failed";
                }
            }

            async function doSettingsChangePassword() {
                const cur = $("sp-curpw").value;
                const np = $("sp-newpw").value;
                const conf = $("sp-confpw").value;
                const msg = $("sp-pw-msg");
                msg.textContent = "";
                msg.className = "sys-msg";
                if (np !== conf) {
                    msg.className = "sys-msg err";
                    msg.textContent = "Passwords do not match";
                    return;
                }
                _suppressAuth = true;
                try {
                    const r = await fetch("/api/auth/change-password", {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json",
                            "X-Requested-With": "lm-chat",
                        },
                        body: JSON.stringify({
                            current_password: cur,
                            new_password: np,
                        }),
                    });
                    const d = await r.json();
                    if (r.ok) {
                        msg.className = "sys-msg ok";
                        msg.textContent = "Password changed";
                        $("sp-curpw").value = "";
                        $("sp-newpw").value = "";
                        $("sp-confpw").value = "";
                    } else {
                        msg.className = "sys-msg err";
                        msg.textContent = d.error || "Failed";
                    }
                } finally {
                    setTimeout(() => {
                        _suppressAuth = false;
                    }, 2000);
                }
            }

            function renderSecurityTab() {
                const u = AUTH_STATE.user || {};
                const enabled = u.totp_enabled;
                if (enabled) {
                    return `
      <div class="sys-section">
        <h3>Two-Factor Authentication</h3>
        <p style="color:var(--green);margin-bottom:var(--sp-7)">&#10003; 2FA is enabled</p>
        <div class="sys-field"><label>Enter current 2FA code to disable</label><input type="text" id="st-code" class="totp-verify-input" maxlength="6" inputmode="numeric" autocomplete="one-time-code" placeholder="000000"></div>
        <button class="sys-btn danger" data-action="disable-totp">Disable 2FA</button>
        <div id="st-msg"></div>
      </div>`;
                }
                return `
    <div class="sys-section">
      <h3>Two-Factor Authentication</h3>
      <p style="color:var(--dim);margin-bottom:var(--sp-7)">Add an extra layer of security to your account</p>
      <div id="totp-setup-area">
        <button class="sys-btn" data-action="start-totp-setup">Enable 2FA</button>
      </div>
      <div id="st-msg"></div>
    </div>`;
            }

            async function startTotpSetup() {
                const area = $("totp-setup-area");
                area.innerHTML =
                    '<p style="color:var(--dim)">Generating secret...</p>';
                const r = await apiFetch("/api/auth/totp/setup", {
                    method: "POST",
                });
                const d = await r.json();
                if (!r.ok) {
                    area.innerHTML = `<p style="color:var(--err-text)">${esc(d.error || "Failed")}</p>`;
                    return;
                }
                _totpSetupToken = d.setup_token;
                area.innerHTML = `
    <div class="totp-qr">
      ${sanitizeSvg(d.qr_svg)}
      <div style="margin-top:var(--sp-6);font-size:var(--text-xs);color:var(--dim)">Scan with your authenticator app</div>
    </div>
    <div style="margin:var(--sp-7) 0">
      <div style="font-size:var(--text-xs);color:var(--dim);margin-bottom:var(--sp-3)">Or enter this secret manually:</div>
      <div class="totp-secret" data-secret="${esc(d.secret)}" data-action="copy-totp-secret" title="Click to copy">${esc(d.secret)}</div>
    </div>
    <div style="margin-top:var(--sp-8)">
      <div style="font-size:var(--text-xs);color:var(--dim);margin-bottom:var(--sp-3)">Enter the 6-digit code from your app to verify:</div>
      <div style="display:flex;gap:var(--sp-5);align-items:center">
        <input type="text" id="st-verify-code" class="totp-verify-input" maxlength="6" inputmode="numeric" autocomplete="one-time-code" placeholder="000000" style="width:10rem">
        <button class="sys-btn" data-action="verify-totp">Verify</button>
      </div>
    </div>`;
                area.querySelector('[data-action="copy-totp-secret"]')?.addEventListener('click', function() {
                    copyToClipboard(this.dataset.secret);
                    this.style.opacity = '.6';
                    setTimeout(() => this.style.opacity = '1', 300);
                });
                area.querySelector('[data-action="verify-totp"]')?.addEventListener('click', verifyTotpSetup);
            }

            async function verifyTotpSetup() {
                const code = $("st-verify-code").value.trim();
                const msg = $("st-msg");
                if (msg) {
                    msg.textContent = "";
                    msg.className = "sys-msg";
                }
                if (code.length !== 6) {
                    if (msg) {
                        msg.className = "sys-msg err";
                        msg.textContent = "Enter a 6-digit code";
                    }
                    return;
                }
                const r = await apiFetch("/api/auth/totp/verify", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        code,
                        setup_token: _totpSetupToken,
                    }),
                });
                const d = await r.json();
                if (r.ok) {
                    AUTH_STATE.user.totp_enabled = 1;
                    renderSettingsTab();
                } else {
                    if (msg) {
                        msg.className = "sys-msg err";
                        msg.textContent = d.error || "Verification failed";
                    }
                }
            }

            async function disableTotp() {
                const code = $("st-code").value.trim();
                const msg = $("st-msg");
                if (msg) {
                    msg.textContent = "";
                    msg.className = "sys-msg";
                }
                if (code.length !== 6) {
                    if (msg) {
                        msg.className = "sys-msg err";
                        msg.textContent = "Enter a 6-digit code";
                    }
                    return;
                }
                const r = await apiFetch("/api/auth/totp/disable", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ code }),
                });
                const d = await r.json();
                if (r.ok) {
                    AUTH_STATE.user.totp_enabled = 0;
                    renderSettingsTab();
                } else {
                    if (msg) {
                        msg.className = "sys-msg err";
                        msg.textContent = d.error || "Invalid code";
                    }
                }
            }
            function renderUsersTab() {
                // Trigger async user loading after render
                setTimeout(loadSettingsUsers, 0);
                return `
    <div class="sys-section">
      <h3>Users</h3>
      <div id="su-list"><p style="color:var(--dim)">Loading...</p></div>
    </div>
    <div class="sys-section">
      <h3>Invite New User</h3>
      <div class="sys-field"><label>Username</label><input type="text" id="su-user" autocomplete="off"></div>
      <div class="sys-field"><label>Display Name</label><input type="text" id="su-name" placeholder="Optional" autocomplete="off"></div>
      <div class="sys-field"><label>Password</label><input type="password" id="su-pass" autocomplete="new-password"></div>
      <button class="sys-btn" data-action="create-user">Create User</button>
      <div id="su-msg"></div>
    </div>`;
            }

            async function loadSettingsUsers() {
                const r = await apiFetch("/api/auth/users");
                if (!r.ok) return;
                const users = await r.json();
                const list = $("su-list");
                if (!list) return;
                list.innerHTML = "";
                if (!users.length) {
                    list.innerHTML = '<p style="color:var(--dim)">No users</p>';
                    return;
                }
                users.forEach((u) => {
                    const d = document.createElement("div");
                    d.className = "user-row";
                    const date = new Date(
                        u.created_at * 1000,
                    ).toLocaleDateString();
                    d.innerHTML =
                        `<div class="ur-name"><strong>${esc(u.display_name || u.username)}</strong><small>@${esc(u.username)} &middot; ${date}</small></div>` +
                        (u.is_admin ?
                            '<span class="ur-badge">admin</span>'
                        :   "");
                    if (u.id !== AUTH_STATE.user.id) {
                        const btn = document.createElement("button");
                        btn.className = "sys-btn danger";
                        btn.style.cssText = "padding:0.3125rem var(--sp-6);font-size:var(--text-xs)";
                        btn.textContent = "Delete";
                        btn.addEventListener("click", () =>
                            settingsDeleteUser(u.id, u.username),
                        );
                        d.appendChild(btn);
                    }
                    list.appendChild(d);
                });
            }

            async function settingsDeleteUser(id, name) {
                const ok = await showDialog({
                    title: "Delete User",
                    message:
                        "Delete user @" +
                        name +
                        "? This will also delete all their chats.",
                    confirmText: "Delete",
                    danger: true,
                });
                if (!ok) return;
                const r = await apiFetch("/api/auth/users/" + id, {
                    method: "DELETE",
                });
                if (r.ok) loadSettingsUsers();
            }

            async function doSettingsInvite() {
                const username = $("su-user").value.trim();
                const password = $("su-pass").value;
                const display_name = $("su-name").value.trim() || username;
                const msg = $("su-msg");
                msg.textContent = "";
                msg.className = "sys-msg";
                if (!username || !password) {
                    msg.className = "sys-msg err";
                    msg.textContent = "Fill in username and password";
                    return;
                }
                const r = await apiFetch("/api/auth/invite", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ username, password, display_name }),
                });
                const d = await r.json();
                if (!r.ok) {
                    msg.className = "sys-msg err";
                    msg.textContent = d.error || "Failed";
                    return;
                }
                msg.className = "sys-msg ok";
                msg.textContent = "User @" + username + " created";
                $("su-user").value = "";
                $("su-pass").value = "";
                $("su-name").value = "";
                loadSettingsUsers();
            }

            // --- API wrapper with 401 handling ---
            let _suppressAuth = false;
            async function apiFetch(url, opts = {}) {
                opts.headers = {
                    "X-Requested-With": "lm-chat",
                    ...(opts.headers || {}),
                };
                const resp = await fetch(url, opts);
                if (
                    resp.status === 401 &&
                    AUTH_STATE.enabled &&
                    !_suppressAuth
                ) {
                    AUTH_STATE.user = null;
                    document.getElementById("user-avatar").classList.add("hidden");
                    const gear = document.getElementById("global-settings-btn");
                    if (gear) gear.classList.remove("hidden");
                    showAuthScreen(false);
                    throw new Error("unauthorized");
                }
                return resp;
            }

            let appVersion = "";

            // Enter key support on auth fields
            document.addEventListener("DOMContentLoaded", () => {
                ["a-user", "a-pass", "a-pass2", "a-name", "a-totp"].forEach(
                    (id) => {
                        const el = document.getElementById(id);
                        if (el)
                            el.addEventListener("keydown", (e) => {
                                if (e.key === "Enter") $("auth-btn").click();
                            });
                    },
                );
            });

            const MCPS = [
                { id: "mcp/brave-search", name: "Brave Search", on: true, hint: "Current events, recent releases, anything after your training cutoff, or when unsure about a fact" },
                { id: "mcp/memory", name: "Memory", on: true, hint: "Store and retrieve persistent user context across conversations" },
                { id: "mcp/sequential-thinking", name: "Sequential Thinking", on: true, hint: "Break down complex problems with structured step-by-step reasoning" },
                { id: "mcp/context7", name: "Context7", on: true, hint: "Look up accurate, up-to-date API docs for libraries and frameworks before writing code" },
                { id: "mcp/paper-search", name: "Paper Search", on: true, hint: "Find academic papers and real research for technical or scientific questions" },
                { id: "mcp/firecrawl", name: "Firecrawl", on: false, hint: "Read full web page content when search snippets aren't enough" },
                { id: "mcp/filesystem", name: "Filesystem", on: false, hint: "Read and write local files" },
                { id: "mcp/playwright", name: "Playwright", on: false, hint: "Browser automation, screenshots, and web interaction" },
                { id: "mcp/github", name: "GitHub", on: false, hint: "Manage GitHub issues, PRs, and repositories" },
            ];

            const STARTER_ICONS = {
                summarize:
                    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><line x1="10" y1="9" x2="8" y2="9"/></svg>',
                explain:
                    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
                brainstorm:
                    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 2A6.5 6.5 0 0 0 3 8.5c0 2.1 1 4 2.6 5.2.4.3.7.8.7 1.3v1a2 2 0 0 0 2 2h3.4a2 2 0 0 0 2-2v-1c0-.5.3-1 .7-1.3A6.5 6.5 0 0 0 9.5 2z"/><path d="M8 18v2"/><path d="M11 18v2"/><path d="M15 7l2-2"/><path d="M17 12h2"/><path d="M15 17l2 2"/></svg>',
                email: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M22 7l-10 7L2 7"/></svg>',
                proscons:
                    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="12" y1="3" x2="12" y2="21"/><line x1="8" y1="8" x2="6" y2="8"/><line x1="10" y1="12" x2="6" y2="12"/><line x1="18" y1="8" x2="14" y2="8"/><line x1="18" y1="12" x2="14" y2="12"/></svg>',
                debug: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/><line x1="14.5" y1="4" x2="9.5" y2="20"/></svg>',
            };
            const DEFAULT_STARTERS = [
                {
                    title: "Summarize",
                    prompt: "Summarize the key points of: ",
                    icon: "summarize",
                },
                {
                    title: "Explain",
                    prompt: "Explain this in simple terms: ",
                    icon: "explain",
                },
                {
                    title: "Brainstorm",
                    prompt: "Give me 10 creative ideas for: ",
                    icon: "brainstorm",
                },
                {
                    title: "Draft email",
                    prompt: "Draft a professional email about: ",
                    icon: "email",
                },
            ];
            function getStarters() {
                const c = localStorage.getItem("lsc-starters");
                if (c)
                    try {
                        return JSON.parse(c);
                    } catch {}
                return DEFAULT_STARTERS;
            }
            function saveStarters(list) {
                localStorage.setItem("lsc-starters", JSON.stringify(list));
            }

            let activeId = null,
                sending = false,
                chatMeta = {},
                incognitoMode = false,
                _totpSetupToken = null;

            // Incognito session history — ephemeral, never persisted.
            // Holds {role, content} pairs so the model has context
            // without server-side response_id chaining.
            const INCOGNITO_HISTORY_MAX = 100; // max turns
            const INCOGNITO_HISTORY_MAX_CHARS = 50000; // max chars in context prefix
            let incognitoHistory = [];

            // --- Session stats (persisted in localStorage) ---
            // GPT-5.2 pricing: $1.75/1M input, $14/1M output (as of March 2026)
            const GPT5_INPUT_PER_TOKEN = 1.75 / 1e6,
                GPT5_OUTPUT_PER_TOKEN = 14 / 1e6;
            function loadSessionStats() {
                try {
                    return (
                        JSON.parse(
                            localStorage.getItem("lsc-session-stats"),
                        ) || { totalIn: 0, totalOut: 0, tpsSum: 0, tpsCount: 0 }
                    );
                } catch {
                    return { totalIn: 0, totalOut: 0, tpsSum: 0, tpsCount: 0 };
                }
            }
            function saveSessionStats(s) {
                localStorage.setItem("lsc-session-stats", JSON.stringify(s));
            }
            function recordTokens(inp, out, tps) {
                const s = loadSessionStats();
                s.totalIn += inp || 0;
                s.totalOut += out || 0;
                if (tps > 0) {
                    s.tpsSum += tps;
                    s.tpsCount++;
                }
                saveSessionStats(s);
                updateSidebarStats(s);
            }
            function updateSidebarStats(s) {
                if (!s) s = loadSessionStats();
                const total = s.totalIn + s.totalOut;
                const el = (id) => document.getElementById(id);
                const tf = el("sf-tokens");
                if (tf)
                    tf.textContent =
                        total >= 1e6 ? (total / 1e6).toFixed(1) + "M"
                        : total >= 1e3 ? (total / 1e3).toFixed(1) + "K"
                        : String(total);
                const tp = el("sf-tps");
                if (tp)
                    tp.textContent =
                        s.tpsCount > 0 ?
                            (s.tpsSum / s.tpsCount).toFixed(1) + " tok/s"
                        :   "—";
                const sv = el("sf-saved");
                if (sv) {
                    const cost =
                        s.totalIn * GPT5_INPUT_PER_TOKEN +
                        s.totalOut * GPT5_OUTPUT_PER_TOKEN;
                    sv.textContent = "$" + cost.toFixed(2);
                }
            }
            let pendingAttachments = [];
            const MAX_IMAGE_SIZE = 20 * 1024 * 1024;
            const ALLOWED_IMAGE_TYPES = [
                "image/jpeg",
                "image/png",
                "image/webp",
                "image/gif",
            ];

            const $ = (id) => document.getElementById(id);
            const scroll = $("scroll"),
                msgs = $("msgs"),
                input = $("input"),
                send = $("send"),
                modelSel = $("model-sel");

            // --- Server settings state ---
            let serverSettings = { hasApiKey: false, lmUrl: "" };

            function renderServerTab() {
                const statusText =
                    serverSettings.hasApiKey ?
                        "API key configured (stored server-side)"
                    :   "No API key set";
                const statusColor =
                    serverSettings.hasApiKey ? "var(--accent)" : "var(--faint)";
                const urlVal = serverSettings.lmUrl || "http://localhost:1234";
                setTimeout(() => {
                    renderModelList();
                    renderMcpList();
                    renderRemoteMcps();
                    loadDebugState();
                    checkConnection();
                }, 0);
                return `
    <div class="sys-section">
      <h3>LM Studio Connection</h3>
      <div class="sys-field"><label>Server URL</label><input type="text" id="ss-url" value="${esc(urlVal)}" placeholder="http://localhost:1234"></div>
      <div class="sys-field"><label>API Key</label><div style="display:flex;gap:var(--sp-4)"><input type="password" id="ss-apikey" placeholder="${serverSettings.hasApiKey ? "••••••••  (saved)" : "Enter API key"}" autocomplete="off" style="flex:1">${serverSettings.hasApiKey ? '<button class="sys-btn" data-action="clear-api-key" style="white-space:nowrap;background:var(--err-bg);color:var(--err-text)">Clear</button>' : ""}<button class="sys-btn" data-action="save-server-settings" style="white-space:nowrap">${serverSettings.hasApiKey ? "Update" : "Save"}</button></div></div>
      <div id="ss-status" style="font-size:var(--text-xs);color:${statusColor};margin-top:var(--sp-4)">${statusText}</div>
      <div id="ss-conn" style="margin-top:var(--sp-6)"></div>
    </div>
    <div class="sys-section">
      <h3>Loaded Models</h3>
      <div id="model-list"></div>
      <div style="font-size:var(--text-xs);color:var(--faint);margin-top:var(--sp-4)">Load and unload models in LM Studio</div>
    </div>
    <div class="sys-section">
      <h3>MCP Tools</h3>
      <div id="mcp-list"></div>
    </div>
    <div class="sys-section">
      <h3>Remote MCP Servers</h3>
      <div id="remote-mcp-list"></div>
      <button data-action="add-remote-mcp" style="background:none;border:1px dashed var(--border);color:var(--dim);width:100%;padding:var(--sp-4);border-radius:0.5rem;cursor:pointer;font-size:var(--text-sm);margin-top:var(--sp-4)">+ Add Remote Server</button>
      <div style="font-size:var(--text-xs);color:var(--faint);margin-top:var(--sp-4)">Requires "Allow per-request MCPs" in LM Studio Developer Settings</div>
    </div>
    <div class="sys-section">
      <h3>Debug Logging</h3>
      <div style="display:flex;align-items:center;gap:var(--sp-6)">
        <label class="sw"><input type="checkbox" id="ss-debug" data-action="toggle-debug"><span class="slider"></span></label>
        <span style="font-size:var(--text-base)">Verbose debug mode</span>
      </div>
      <div id="ss-debug-info" style="font-size:var(--text-xs);color:var(--faint);margin-top:var(--sp-4)">Logs requests, SSE events, memory operations, and tool calls to <code style="font-size:0.6875rem">logs/</code></div>
      <div id="ss-log-files" style="margin-top:var(--sp-4)"></div>
    </div>`;
            }

            async function saveServerSettings() {
                const url = $("ss-url").value.trim();
                const key = $("ss-apikey").value.trim();
                const status = $("ss-status");
                const payload = {};
                if (url) payload.lm_url = url;
                if (key) payload.lm_apikey = key;
                // Don't send lm_apikey='' when field is empty — that would clear the saved key
                // Use clearApiKey() to explicitly remove a key
                const r = await apiFetch("/api/auth/settings", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
                if (r.ok) {
                    status.textContent = "Settings saved";
                    status.style.color = "var(--accent)";
                    $("ss-apikey").value = "";
                    if (key) serverSettings.hasApiKey = true;
                    if (url) serverSettings.lmUrl = url;
                    checkConnection();
                    refreshModels();
                } else {
                    status.textContent = "Failed to save";
                    status.style.color = "var(--err-text)";
                }
            }

            async function clearApiKey() {
                const r = await apiFetch("/api/auth/settings", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ lm_apikey: "" }),
                });
                if (r.ok) {
                    serverSettings.hasApiKey = false;
                    const status = $("ss-status");
                    if (status) {
                        status.textContent = "API key cleared";
                        status.style.color = "var(--faint)";
                    }
                    openSettings("server");
                    checkConnection();
                    refreshModels();
                }
            }

            async function loadDebugState() {
                try {
                    const r = await apiFetch("/api/debug");
                    if (!r.ok) return;
                    const d = await r.json();
                    const cb = $("ss-debug");
                    if (cb) cb.checked = d.enabled;
                    const el = $("ss-log-files");
                    if (el && d.log_files && d.log_files.length) {
                        el.innerHTML = d.log_files
                            .map(
                                (f) =>
                                    `<div style="font-size:0.6875rem;color:var(--dim);padding:var(--sp-1) 0"><code>${esc(f.name)}</code> — ${(f.size / 1024).toFixed(1)} KB</div>`,
                            )
                            .join("");
                    }
                } catch (e) {
                    console.error("loadDebugState:", e);
                }
            }
            async function toggleDebugMode(on) {
                try {
                    const r = await apiFetch("/api/debug", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ enabled: on }),
                    });
                    if (!r.ok) {
                        const cb = $("ss-debug");
                        if (cb) cb.checked = !on;
                    } else loadDebugState();
                } catch (e) {
                    const cb = $("ss-debug");
                    if (cb) cb.checked = !on;
                }
            }

            // API key is stored server-side via saveServerSettings(), never in localStorage
            async function loadSettings() {
                try {
                    const r = await apiFetch("/api/auth/settings");
                    if (!r.ok) return;
                    const d = await r.json();
                    serverSettings.hasApiKey = !!d.lm_apikey;
                    serverSettings.lmUrl = d.lm_url || "";
                    if (d.remote_mcps) {
                        remoteMcps = d.remote_mcps;
                        renderRemoteMcps();
                    }
                } catch (e) {
                    console.error("loadSettings:", e);
                }
            }

            // --- Connection status ---
            const connDot = document.querySelector("#conn-status .status-dot");
            function setConn(state, text) {
                connDot.className = "status-dot " + state;
                $("conn-status").title =
                    state === "green" ? "Connected — " + text
                    : state === "yellow" ? "Connecting..."
                    : "Disconnected";
                const sc = $("ss-conn");
                if (sc) {
                    if (state === "green")
                        sc.innerHTML =
                            '<span style="color:var(--green)">&#10003; Connected to LM Studio' +
                            (text ? " — " + esc(text) : "") +
                            "</span>";
                    else if (state === "yellow")
                        sc.innerHTML =
                            '<span style="color:var(--dim)">Connecting...</span>';
                    else
                        sc.innerHTML =
                            '<span style="color:var(--err-text)">&#10007; Not connected — is LM Studio running?</span>';
                }
            }

            async function checkConnection() {
                setConn("yellow", "...");
                try {
                    await refreshModels();
                } catch (e) {
                    setConn("red", "Disconnected");
                    if (!modelSel.options.length)
                        modelSel.innerHTML = "<option>qwen3.5-9b</option>";
                }
            }
            // checkConnection interval is started inside initApp after auth check
            modelSel.onchange = () => {
                localStorage.setItem("lsc-model", modelSel.value);
                if (activeId && chatMeta[activeId])
                    chatMeta[activeId].response_id = null;
                updateModelPill();
                updateTopModelLabel();
                syncModelSettings();
            };

            function detectModelFamily(id) {
                const m = (id || "").toLowerCase();
                if (/qwen|qwq/.test(m)) return "qwen";
                if (/llama|meta-llama/.test(m)) return "llama";
                if (/mistral|mixtral/.test(m)) return "mistral";
                if (/deepseek/.test(m)) return "deepseek";
                if (/gemma/.test(m)) return "gemma";
                if (/phi-[34]/.test(m)) return "phi";
                return "default";
            }
            function syncModelSettings() {
                const m = cachedModels.find((x) => x.id === modelSel.value);
                if (!m) return;
                // Auto-enable reasoning for known reasoning models
                const family = detectModelFamily(m.id);
                if (
                    ["qwen", "deepseek"].includes(family) &&
                    /\br1\b|\bqwq\b/i.test(m.id)
                ) {
                    const el = $("s-reasoning");
                    if (el && el.value === "off") el.value = "medium";
                }
                // Sync context length from LM Studio model config
                if (m.context_length) {
                    $("s-ctx").value = m.context_length;
                    localStorage.setItem("lsc-ctx", String(m.context_length));
                }
                if (m.max_context_length) $("s-ctx").max = m.max_context_length;
                // Sync presence_penalty from instance config if LM Studio exposes it
                const cfg2 = m.instance_config || {};
                if (cfg2.presence_penalty != null) {
                    $("s-presence-pen").value = cfg2.presence_penalty;
                    localStorage.setItem("lsc-presence-pen", String(cfg2.presence_penalty));
                }
                // Show instance config (read-only info from LM Studio)
                const cfg = m.instance_config || {};
                const el = $("model-config-info");
                if (!el) return;
                const caps = m.capabilities || {};
                const tags = [];
                if (caps.vision) tags.push("Vision");
                if (caps.trained_for_tool_use) tags.push("Tool Use");
                if (cfg.flash_attention) tags.push("Flash Attn");
                if (cfg.offload_kv_cache_to_gpu) tags.push("KV→GPU");
                const parts = [];
                if (m.context_length)
                    parts.push(
                        `Context: ${(m.context_length / 1024).toFixed(0)}K`,
                    );
                if (cfg.eval_batch_size)
                    parts.push(`Eval Batch: ${cfg.eval_batch_size}`);
                if (cfg.parallel) parts.push(`Parallel: ${cfg.parallel}`);
                el.innerHTML =
                    parts.length || tags.length ?
                        `<span class="mci-stats">${parts.join(" · ")}</span>` +
                        (tags.length ?
                            `<span class="mci-tags">${tags.map((t) => `<span class="mci-tag">${t}</span>`).join("")}</span>`
                        :   "")
                    :   "";
            }

            // --- Settings (localStorage only — these are per-device prefs) ---
            function lss(id, key, def) {
                const v = localStorage.getItem(key);
                $(id).value = v || def;
                $(id).onchange = () => localStorage.setItem(key, $(id).value);
            }
            lss("s-sys", "lsc-sys", "");
            lss("s-temp", "lsc-temp", "0.7");
            lss("s-ctx", "lsc-ctx", "");
            lss("s-top-p", "lsc-top-p", "0.95");
            lss("s-top-k", "lsc-top-k", "40");
            lss("s-min-p", "lsc-min-p", "0.05");
            lss("s-repeat-pen", "lsc-repeat-pen", "1.0");
            lss("s-presence-pen", "lsc-presence-pen", "1");
            lss("s-max-tokens", "lsc-max-tokens", "-1");

            // Load memory toggle state
            apiFetch("/api/insights/settings")
                .then((r) => r.json())
                .then((d) => {
                    const el = $("s-memory");
                    if (el) el.checked = d.memory_enabled !== "false";
                })
                .catch(() => {});

            // --- System Prompt Presets ---
            const PRESETS = {
                general: `You are a sharp, knowledgeable assistant with deep technical expertise and broad intellectual curiosity. Today is {{current_date}}.

## PERSONALITY

You're the friend who happens to know a lot about everything — software, hardware, science, philosophy, music, books, life. You talk like a real person.

- Be direct and honest. If something is a bad idea, say so.
- Have opinions. Say what you actually think, then explain why.
- Push back when the user is wrong. Respect is telling someone the truth, not agreeing with them.
- Match the user's energy. Casual question gets a casual answer. Deep question gets depth.
- Be funny when it's natural, not forced.

## TECHNICAL CONVERSATIONS

- Calibrate to the user's level. Skip basics they already know.
- Explain ML, inference, GPU, and hardware concepts naturally as they come up.
- Use analogies from web dev, databases, gaming, or music production when they fit.

{{tools}}

## RESPONSE STYLE

- Answer first, then elaborate. Lead with your recommendation.
- Keep responses proportional to the question. One-line questions get concise answers.
- State what you know confidently. When uncertain, say so and look it up.
- Disagree when you have reason to. Sycophancy is not helpful.`,

                coder: `You are an expert software engineering agent. Today is {{current_date}}.

## PRINCIPLES

1. Read files before editing them. Understand existing structure first.
2. Verify API signatures against current documentation to ensure accuracy.
3. Plan the approach before writing code.
4. Write complete, working functions. Every function you produce must be ready to run.
5. Look up imports and method signatures to verify them.

{{tools}}

## WORKFLOW

1. **Understand** — Read relevant files. Learn the existing patterns.
2. **Plan** — What changes are needed? What could go wrong?
3. **Research** — Verify APIs and signatures against current docs.
4. **Implement** — Write complete code. Match existing style.
5. **Verify** — Read back modified files. Check for missing imports, edge cases.

## CODE STANDARDS

- Match the existing codebase's style and conventions.
- Write the minimum code needed to solve the problem correctly.
- Keep imports clean. Remove dead code. Avoid speculative abstractions.
- Handle errors at system boundaries, not against impossible internal states.`,

                creative: `You are a skilled writer and creative collaborator. Today is {{current_date}}.

## WHO YOU ARE

You write like someone who's read everything and remembers what worked. You know craft — structure, rhythm, voice, tension, subtext — and you deploy it without showing off. You're a co-writer, not a content generator.

## VOICE

- Write like a human. No AI slop. No "the silence was deafening." No "a testament to." If a phrase sounds like it came from a corporate blog, kill it.
- Favor concrete over abstract. "He hadn't eaten since Tuesday" hits harder than "he was consumed by hunger."
- Vary sentence length. Short sentences punch. Longer ones build rhythm and carry the reader through a thought.
- Trust the reader. Don't explain the emotion — create the conditions for it.
- Kill adverbs unless they earn their spot.

## COLLABORATION STYLE

- When the user shares writing, respond as a thoughtful workshop partner. Name what's working first, then identify what isn't and why.
- Don't rewrite their voice into yours. Strengthen THEIR voice.
- Offer specific craft suggestions, not vague praise.
- If they ask you to write something, ask about tone, audience, and intent before drafting. Then write ONE strong version.

## WHEN GENERATING

- Open strong. No throat-clearing. Start where the energy is.
- End stronger. The last line is what the reader carries away.
- Dialogue should sound like people actually talk — interruptions, deflections, things left unsaid.
- Surprise yourself. If you know where a sentence is going, the reader does too.

## STANDARDS

- Write like a human. You know the difference.
- Use metaphor sparingly. One good metaphor per page beats five per paragraph.
- Let endings be earned, not defaulted. Tidy resolutions need justification.
- Write what the story needs, including darkness.`,

                research: `You are a deep research agent. Today is {{current_date}}.

## APPROACH
Research every question using your tools before answering. Verify with current sources to ensure accuracy. Search first, synthesize second, answer third.

{{tools}}

## RESEARCH PROCESS

1. **Think first** — What do I need to find out? What are the sub-questions?
2. **Search broadly** — Multiple queries, different angles. Use every relevant search tool available.
3. **Read deeply** — Full pages, not snippets. Minimum 3 sources.
4. **Identify gaps** — What's still missing? Any contradictions?
5. **Search again** if gaps exist
6. **Synthesize** — Combine findings into a clear, organized response

## RESPONSE STYLE

- Be detailed and thorough. Depth over brevity.
- Cite your sources. Every factual claim should trace back to a source you actually read.
- Surface contradictions between sources explicitly.
- Suggest angles the user didn't think of.
- Flag when evidence is thin or inconclusive rather than presenting speculation as fact.`,

                analyst: `You are a strategic analyst. You take raw information and turn it into clear analysis and actionable plans. Today is {{current_date}}.

## YOUR ROLE

1. Read and deeply understand what you're given
2. Find the patterns, contradictions, and connections others missed
3. Form a concrete opinion — not a balanced summary
4. Formulate a clear strategy with concrete next steps

## PRINCIPLES

- Produce new insights, not summaries. Synthesis connects dots across sources.
- Identify what's missing. Surface unverified assumptions.
- Surface contradictions — that's where the interesting analysis lives.
- Rank by impact. Say what matters most and why.
- Include invalidation criteria. Every conclusion states what would prove it wrong.
- End with concrete next steps.

## ANALYTICAL FRAMEWORK

1. **Situational Assessment** — Core dynamics in 2-3 sentences
2. **Key Findings** (3-5 max) — Synthesized insights connecting dots across sources
3. **Multi-Dimensional Analysis** — Technical feasibility, economic impact, timeline dynamics, competitive landscape, dependencies
4. **Tensions and Contradictions** — Where sources conflict, assess which side has stronger evidence
5. **Gap Analysis** — What's missing? Rank by impact on conclusion
6. **Strategic Options** — 2-3 viable paths with tradeoffs and invalidation triggers
7. **Recommendation** — Pick one. Say why. First concrete step, success signal, reassess trigger.

## CONFIDENCE TAGGING

Tag every major claim: **High confidence** / **Moderate confidence** / **Low confidence / Speculative** / **Unverified assumption**

## RESPONSE STYLE

- Analyze, not summarize. Prioritize ruthlessly.
- Lead with your recommendation, then support it.
- State your confidence level. When evidence is thin, say so.`,

                architect: `You are a systems architect. You turn strategic analysis, requirements, and research findings into concrete technical plans. Today is {{current_date}}.

## YOUR ROLE

You are the bridge between "what should we do" and "how exactly do we build it." Your deliverables:
1. Architecture decisions with explicit rationale
2. Component breakdowns with clear boundaries and interfaces
3. Technology selections justified against alternatives
4. Phased implementation plans that can be handed to a developer

## PRINCIPLES

- Every decision includes rationale and what alternatives were rejected.
- Ask what the user already has before proposing architecture.
- Design for what's needed now. Over-engineering is a bug.
- Every component has a single responsibility and a defined interface.
- Name the tradeoffs. If you can't name what you're giving up, you don't understand the choice.

## ARCHITECTURAL PROCESS

1. **Constraints Inventory** — Resources, technical limits, team skills, non-negotiables
2. **System Overview** — One paragraph. What it does end to end.
3. **Component Architecture** — Name, responsibility, inputs/outputs, interface, key detail, risk
4. **Technology Selection** — What, why, what was rejected, lock-in risk, verification step
5. **Phased Roadmap** — Each phase produces something runnable with testable done criteria
6. **Risk Register** — Top 3-5 risks with likelihood, impact, and mitigation

## THINKING STYLE

- Think in interfaces, not implementations. Define boundaries first.
- Think in failure modes. What happens when this component is slow or unavailable?
- Think in iterations. The first version should be embarrassingly simple.
- Use well-known patterns. Name them so developers can look them up.

## RESPONSE STYLE

- Understand before designing. Read existing code first.
- Be precise about interfaces — format, protocol, failure behavior.
- Estimate complexity (small/medium/large), not time.`,
            };

            // --- Prompt Variables ---
            function expandVars(text) {
                const now = new Date();
                const days = [
                    "Sunday",
                    "Monday",
                    "Tuesday",
                    "Wednesday",
                    "Thursday",
                    "Friday",
                    "Saturday",
                ];
                return text
                    .replace(
                        /\{\{current_date\}\}/g,
                        now.toISOString().slice(0, 10),
                    )
                    .replace(/\{\{day_of_week\}\}/g, days[now.getDay()])
                    .replace(
                        /\{\{current_time\}\}/g,
                        now.toLocaleTimeString("en-US", {
                            hour: "2-digit",
                            minute: "2-digit",
                            hour12: true,
                        }),
                    )
                    .replace(/\{\{model\}\}/g, modelSel.value || "unknown")
                    .replace(/\{\{tools\}\}/g, "") // LM Studio injects tool schemas server-side; {{tools}} expands to empty
                    .replace(/\{\{memories\}\}/g, ""); // handled server-side
            }

            function applyPreset() {
                const v = $("s-preset").value;
                if (v === "custom") {
                    $("s-sys").value = "";
                    localStorage.setItem("lsc-sys", "");
                    localStorage.setItem("lsc-preset", "custom");
                    return;
                }
                if (PRESETS[v]) {
                    $("s-sys").value = PRESETS[v];
                    localStorage.setItem("lsc-sys", PRESETS[v]);
                    localStorage.setItem("lsc-preset", v);
                }
            }
            // Track manual edits — switch dropdown to "Custom"
            $("s-sys").addEventListener("input", () => {
                $("s-preset").value = "custom";
                localStorage.setItem("lsc-sys", $("s-sys").value);
                localStorage.setItem("lsc-preset", "custom");
            });
            // On load, restore preset selection
            (function initPreset() {
                const saved = localStorage.getItem("lsc-preset");
                if (
                    saved &&
                    Array.from($("s-preset").options).some(
                        (o) => o.value === saved,
                    )
                ) {
                    $("s-preset").value = saved;
                    if (saved !== "custom" && PRESETS[saved])
                        $("s-sys").value = PRESETS[saved];
                } else {
                    // First load — populate with default preset
                    const def = $("s-preset").value;
                    if (PRESETS[def]) $("s-sys").value = PRESETS[def];
                }
            })();
            $("s-reasoning").value =
                localStorage.getItem("lsc-reasoning") || "off";
            $("s-followups").checked =
                localStorage.getItem("lsc-followups") !== "off";
            // API key — loaded from server via loadSettings(), saved via saveServerSettings()
            // Remove any legacy localStorage key on load
            localStorage.removeItem("lsc-apikey");

            // --- MCP toggles ---
            MCPS.forEach((s, i) => {
                const v = localStorage.getItem("lsc-mcp-" + s.id);
                if (v !== null) s.on = v === "1";
            });
            function renderMcpList() {
                const list = $("mcp-list");
                if (!list) return;
                list.innerHTML = "";
                MCPS.forEach((s, i) => {
                    const d = document.createElement("div");
                    d.className = "mcp-i";
                    d.innerHTML = `<input type=checkbox ${s.on ? "checked" : ""}><span>${esc(s.name)}</span>`;
                    d.querySelector("input").onchange = (e) => {
                        MCPS[i].on = e.target.checked;
                        localStorage.setItem(
                            "lsc-mcp-" + s.id,
                            e.target.checked ? "1" : "0",
                        );
                    };
                    list.appendChild(d);
                });
            }
            renderMcpList();

            // --- Remote MCP servers (H4: auth stored server-side) ---
            let remoteMcps = [];
            // Migrate: clear legacy localStorage data
            localStorage.removeItem("lsc-remote-mcps");
            function saveRemoteMcps() {
                // Save to server — send label/url/on but NOT auth (auth only sent when explicitly set)
                const payload = remoteMcps.map((s) => ({
                    label: s.label,
                    url: s.url,
                    on: s.on,
                }));
                apiFetch("/api/auth/settings", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ remote_mcps: payload }),
                }).catch(() => {});
            }
            function renderRemoteMcps() {
                const list = $("remote-mcp-list");
                if (!list) return;
                list.innerHTML = "";
                remoteMcps.forEach((s, i) => {
                    const d = document.createElement("div");
                    d.className = "mcp-i";
                    const authBadge =
                        s.has_auth ?
                            '<span style="color:var(--accent);font-size:0.625rem;margin-left:var(--sp-3)" title="Auth token configured">&#128274;</span>'
                        :   "";
                    d.innerHTML = `<input type=checkbox ${s.on ? "checked" : ""}><span>${esc(s.label)}</span>${authBadge}<span style="color:var(--faint);font-size:0.6875rem;margin-left:auto">${esc(s.url)}</span><button data-action="set-mcp-auth" data-idx="${i}" style="background:none;border:none;color:var(--dim);cursor:pointer;font-size:var(--text-xs);padding:0 var(--sp-2)" title="Set auth token">&#128273;</button><button data-action="remove-remote-mcp" data-idx="${i}" style="background:none;border:none;color:var(--dim);cursor:pointer;font-size:var(--text-lg);padding:0 var(--sp-2)" title="Remove">&times;</button>`;
                    d.querySelector('[data-action="set-mcp-auth"]').addEventListener('click', () => setMcpAuth(i));
                    d.querySelector('[data-action="remove-remote-mcp"]').addEventListener('click', () => removeRemoteMcp(i));
                    d.querySelector("input").onchange = (e) => {
                        remoteMcps[i].on = e.target.checked;
                        saveRemoteMcps();
                    };
                    list.appendChild(d);
                });
            }
            async function addRemoteMcp() {
                const vals = await showDialog({
                    title: "Add Remote MCP Server",
                    fields: [
                        {
                            label: "Label",
                            placeholder: "e.g. my-tools",
                            required: true,
                        },
                        {
                            label: "Server URL",
                            placeholder: "e.g. https://my-server.com/mcp",
                            required: true,
                        },
                        {
                            label: "Authorization header (optional)",
                            placeholder: "Bearer ...",
                        },
                    ],
                    confirmText: "Add",
                });
                if (!vals || !vals[0] || !vals[1]) return;
                const [label, url, auth] = vals;
                remoteMcps.push({ label, url, on: true, has_auth: !!auth });
                const payload = remoteMcps.map((s) => ({
                    label: s.label,
                    url: s.url,
                    on: s.on,
                }));
                payload[payload.length - 1].auth = auth || "";
                apiFetch("/api/auth/settings", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ remote_mcps: payload }),
                })
                    .then(async (r) => {
                        if (r.ok) {
                            const d = await r.json();
                            if (d.remote_mcps) remoteMcps = d.remote_mcps;
                            renderRemoteMcps();
                        }
                    })
                    .catch(() => {});
                renderRemoteMcps();
            }
            async function setMcpAuth(i) {
                const s = remoteMcps[i];
                const auth = await showDialog({
                    title: "Set Auth Token",
                    message:
                        'Set auth token for "' +
                        s.label +
                        '" (leave empty to clear):',
                    input: true,
                    placeholder: "Bearer ...",
                    confirmText: "Save",
                });
                if (auth === null) return;
                // Send full list with auth only for the changed entry
                const payload = remoteMcps.map((m, j) => ({
                    label: m.label,
                    url: m.url,
                    on: m.on,
                    ...(j === i ? { auth } : {}),
                }));
                apiFetch("/api/auth/settings", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ remote_mcps: payload }),
                })
                    .then(async (r) => {
                        if (r.ok) {
                            const d = await r.json();
                            if (d.remote_mcps) remoteMcps = d.remote_mcps;
                            renderRemoteMcps();
                        }
                    })
                    .catch(() => {});
            }
            function removeRemoteMcp(i) {
                remoteMcps.splice(i, 1);
                saveRemoteMcps();
                renderRemoteMcps();
            }
            renderRemoteMcps();

            // --- Chat Search ---
            function filterChats() {
                const q = $("chat-search").value.toLowerCase();
                $("chat-search-clear").style.display = q ? "block" : "none";
                const items = $("chat-list").children;
                let visible = 0;
                for (const item of items) {
                    if (item.classList.contains("sidebar-section")) {
                        item.style.display = q ? "none" : "";
                        continue;
                    }
                    const title =
                        item
                            .querySelector("span")
                            ?.textContent?.toLowerCase() || "";
                    const show = !q || title.includes(q);
                    item.style.display = show ? "" : "none";
                    if (show) visible++;
                }
                $("chat-no-match").style.display =
                    q && !visible ? "block" : "none";
            }
            let searchMode = "text";

            $("chat-search").addEventListener("keydown", async (e) => {
                if (e.key === "Enter" && searchMode === "semantic") {
                    e.preventDefault();
                    const q = $("chat-search").value.trim();
                    if (!q) return;
                    try {
                        const r = await apiFetch("/api/search", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ query: q }),
                        });
                        const d = await r.json();
                        if (d.mode === "unavailable") {
                            searchMode = "text";
                            filterChats();
                            return;
                        }
                        renderSearchResults(d.results || []);
                    } catch (e) {
                        filterChats();
                    }
                }
            });

            function renderSearchResults(results) {
                const list = $("chat-list");
                list.innerHTML = "";
                if (!results.length) {
                    $("chat-no-match").style.display = "block";
                    $("chat-no-match").textContent = "No semantic matches";
                    return;
                }
                $("chat-no-match").style.display = "none";
                results.forEach((r) => {
                    const d = document.createElement("div");
                    d.className = "sr";
                    d.innerHTML = `<div class="sr-title">${esc(r.chat_title || "Untitled")}<span class="sr-score">${(r.score * 100).toFixed(0)}%</span></div><div class="sr-text">${esc(r.content)}</div>`;
                    d.onclick = () => {
                        loadChat(r.chat_id);
                        clearChatSearch();
                    };
                    list.appendChild(d);
                });
            }

            function toggleSearchMode() {
                searchMode = searchMode === "text" ? "semantic" : "text";
                $("search-mode").textContent =
                    searchMode === "semantic" ?
                        "Semantic search (Enter to search)"
                    :   "Text search (Enter for semantic)";
                if (searchMode === "text") filterChats();
            }

            function clearChatSearch() {
                $("chat-search").value = "";
                searchMode = "text";
                $("search-mode").textContent =
                    "Text search (Enter for semantic)";
                filterChats();
                $("chat-search").focus();
            }

            // --- Folder context menu ---
            $("chat-list").addEventListener("contextmenu", async (e) => {
                const ci = e.target.closest(".ci");
                if (!ci) return;
                e.preventDefault();
                const id = ci.dataset.id;
                const current = (chatMeta[id] || {}).folder || "";
                const name = await showDialog({
                    title: "Set Folder",
                    input: true,
                    placeholder: "Folder name (empty to remove)",
                    inputValue: current,
                    confirmText: "Save",
                });
                if (name === null) return;
                apiFetch("/api/chats/" + id + "/folder", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ folder: name }),
                })
                    .then((r) => r.json())
                    .then((data) => {
                        chatMeta[id].folder = data.folder;
                        renderList();
                    });
            });

            // --- Sidebar ---
            function openSB() {
                ignoreScrollEvent = true;
                document.body.classList.remove("sb-closed");
                if (window.innerWidth <= 768)
                    $("sb-overlay").style.display = "block";
                setTimeout(() => {
                    ignoreScrollEvent = false;
                }, 100);
            }
            function closeSB() {
                ignoreScrollEvent = true;
                document.body.classList.add("sb-closed");
                $("sb-overlay").style.display = "none";
                setTimeout(() => {
                    ignoreScrollEvent = false;
                }, 100);
            }
            window.addEventListener("resize", () => {
                if (window.innerWidth > 768)
                    $("sb-overlay").style.display = "none";
            });

            // --- Chat list (from server) ---
            async function loadChatList() {
                try {
                    const resp = await apiFetch("/api/chats");
                    if (!resp.ok) throw new Error("Failed to load chats");
                    const chats = await resp.json();
                    chatMeta = {};
                    chats.forEach((c) => (chatMeta[c.id] = c));
                    renderList();
                } catch (e) {
                    console.error("loadChatList:", e);
                    const el = $("chat-list");
                    el.innerHTML =
                        '<div style="padding:var(--sp-7);color:var(--err-text);font-size:var(--text-sm)">Failed to load chats. <button class="retry-btn" style="color:var(--accent);background:none;border:none;cursor:pointer;text-decoration:underline">Retry</button></div>';
                    el.querySelector(".retry-btn")?.addEventListener(
                        "click",
                        () => loadChatList(),
                    );
                }
            }

            function renderList() {
                const list = $("chat-list");
                list.innerHTML = "";
                const chats = Object.values(chatMeta)
                    .filter((c) => !c._incognito)
                    .sort((a, b) => {
                        if ((a.pinned || 0) !== (b.pinned || 0))
                            return (b.pinned || 0) - (a.pinned || 0);
                        return (b.updated_at || 0) - (a.updated_at || 0);
                    });
                const pinned = chats.filter((c) => c.pinned);
                const folders = {};
                const unfiled = [];
                chats
                    .filter((c) => !c.pinned)
                    .forEach((c) => {
                        if (c.folder)
                            (folders[c.folder] = folders[c.folder] || []).push(
                                c,
                            );
                        else unfiled.push(c);
                    });
                function addItem(c) {
                    const d = document.createElement("div");
                    d.className = "ci" + (c.id === activeId ? " active" : "");
                    d.dataset.id = c.id;
                    const pinCls = c.pinned ? "pin pinned" : "pin";
                    const pinIcon = c.pinned ? "\u2605" : "\u2606";
                    d.innerHTML = `<span>${esc(c.title)}</span><button class="${pinCls}" title="${c.pinned ? "Unpin" : "Pin"}">${pinIcon}</button><button class="del">&times;</button>`;
                    d.querySelector(".pin").onclick = (e) => {
                        e.stopPropagation();
                        togglePin(c.id);
                    };
                    d.querySelector(".del").onclick = (e) => {
                        e.stopPropagation();
                        deleteChat(c.id);
                    };
                    d.setAttribute("role", "button");
                    d.setAttribute("tabindex", "0");
                    d.onclick = () => loadChat(c.id);
                    d.addEventListener("keydown", (e) => {
                        if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            loadChat(c.id);
                        }
                    });
                    list.appendChild(d);
                }
                if (pinned.length) {
                    const h = document.createElement("div");
                    h.className = "sidebar-section";
                    h.textContent = "Pinned";
                    list.appendChild(h);
                    pinned.forEach(addItem);
                }
                Object.keys(folders)
                    .sort()
                    .forEach((name) => {
                        const collapsed =
                            localStorage.getItem("lsc-folder-" + name) === "1";
                        const h = document.createElement("div");
                        h.className = "sidebar-section folder-hdr";
                        h.setAttribute("role", "button");
                        h.setAttribute("tabindex", "0");
                        h.innerHTML =
                            '<span class="folder-name">' +
                            esc(name) +
                            "</span>" +
                            '<span class="folder-arrow">' +
                            (collapsed ? "\u25B8" : "\u25BE") +
                            "</span>";
                        h.onclick = () => toggleFolder(name);
                        h.onkeydown = (e) => {
                            if (e.key === "Enter" || e.key === " ") {
                                e.preventDefault();
                                toggleFolder(name);
                            }
                        };
                        list.appendChild(h);
                        if (!collapsed) folders[name].forEach(addItem);
                    });
                if (unfiled.length) {
                    if (pinned.length || Object.keys(folders).length) {
                        const h = document.createElement("div");
                        h.className = "sidebar-section";
                        h.textContent = "Recent";
                        list.appendChild(h);
                    }
                    unfiled.forEach(addItem);
                }
                if (!chats.length) {
                    const empty = document.createElement("div");
                    empty.style.cssText =
                        "padding:1.5rem var(--sp-6);text-align:center;font-size:var(--text-sm);color:var(--faint)";
                    empty.textContent = "No conversations yet";
                    list.appendChild(empty);
                }
                filterChats();
            }

            async function togglePin(id) {
                const res = await apiFetch("/api/chats/" + id + "/pin", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                });
                const data = await res.json();
                chatMeta[id].pinned = data.pinned;
                renderList();
            }

            function toggleFolder(name) {
                const key = "lsc-folder-" + name;
                localStorage.setItem(
                    key,
                    localStorage.getItem(key) === "1" ? "0" : "1",
                );
                renderList();
            }

            // --- Memory: distillation ---
            const _distilling = new Set();

            async function distillChat(chatId) {
                if (
                    !chatId ||
                    _distilling.has(chatId) ||
                    incognitoMode ||
                    (chatId && chatId.startsWith("incog_"))
                )
                    return;
                const meta = chatMeta[chatId];
                if (!meta) return;
                _distilling.add(chatId);
                try {
                    const resp = await apiFetch("/api/insights/distill", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            chat_id: chatId,
                            model: modelSel.value,
                        }),
                    });
                    if (!resp.ok) return;
                    const data = await resp.json();
                    if (data.insights && data.insights.length > 0) {
                        showMemoryBadge(data.insights.length);
                    }
                } catch (e) {
                    console.error("distill:", e);
                } finally {
                    _distilling.delete(chatId);
                }
            }

            let _badgeTimer = null;
            function showMemoryBadge(count) {
                let badge = $("memory-badge");
                if (!badge) {
                    badge = document.createElement("div");
                    badge.id = "memory-badge";
                    badge.onclick = () => {
                        openSettings("memory");
                        badge.remove();
                    };
                    document.querySelector(".topbar-r").prepend(badge);
                }
                badge.textContent =
                    count + " new insight" + (count !== 1 ? "s" : "");
                badge.classList.add("visible");
                if (_badgeTimer) clearTimeout(_badgeTimer);
                _badgeTimer = setTimeout(() => {
                    if (badge) badge.remove();
                    _badgeTimer = null;
                }, 8000);
            }

            // --- Memory panel ---
            async function loadMemoryPanel() {
                const list = $("memory-list");
                if (!list) return;
                try {
                    const resp = await apiFetch("/api/insights");
                    if (!resp.ok) return;
                    const insights = await resp.json();
                    if (!insights.length) {
                        list.innerHTML =
                            '<div style="padding:var(--sp-6);color:var(--faint);font-size:var(--text-xs);text-align:center">No memories yet. Chat a bit and switch chats to start building memory.</div>';
                        return;
                    }
                    list.innerHTML = "";
                    function renderInsight(i, container, faded) {
                        const d = document.createElement("div");
                        d.className = "mem-item";
                        if (faded) d.style.opacity = ".5";
                        const cat = document.createElement("span");
                        cat.className = "mem-cat " + i.category;
                        cat.textContent = i.category;
                        const txt = document.createElement("span");
                        txt.className = "mem-text";
                        txt.textContent = i.content;
                        const del = document.createElement("button");
                        del.className = "mem-del";
                        del.title = "Delete";
                        del.textContent = "\u00d7";
                        del.addEventListener("click", () =>
                            deleteInsight(i.id),
                        );
                        d.append(cat, txt, del);
                        container.appendChild(d);
                    }
                    insights
                        .filter((i) => i.state === "active")
                        .forEach((i) => renderInsight(i, list, false));
                    const faded = insights.filter((i) => i.state === "faded");
                    if (faded.length) {
                        const hdr = document.createElement("div");
                        hdr.style.cssText =
                            "font-size:0.625rem;color:var(--faint);padding:var(--sp-4) var(--sp-5) var(--sp-2);text-transform:uppercase;letter-spacing:.5px";
                        hdr.textContent = `Faded (${faded.length})`;
                        list.appendChild(hdr);
                        faded.forEach((i) => renderInsight(i, list, true));
                    }
                } catch (e) {
                    list.innerHTML =
                        '<div style="color:var(--err-text);font-size:var(--text-xs)">Failed to load</div>';
                }
            }

            async function deleteInsight(id) {
                try {
                    const resp = await apiFetch(
                        `/api/insights/${encodeURIComponent(id)}`,
                        { method: "DELETE" },
                    );
                    if (!resp.ok)
                        console.error("delete insight failed:", resp.status);
                } catch (e) {
                    console.error("delete insight:", e);
                }
                loadMemoryPanel();
            }

            async function clearInsights() {
                if (
                    !(await showDialog({
                        title: "Clear Memories",
                        message: "Delete all memories? This cannot be undone.",
                        confirmText: "Delete All",
                        danger: true,
                    }))
                )
                    return;
                try {
                    const resp = await apiFetch("/api/insights", {
                        method: "DELETE",
                    });
                    if (!resp.ok)
                        console.error("clear insights failed:", resp.status);
                } catch (e) {
                    console.error("clear insights:", e);
                }
                loadMemoryPanel();
            }

            async function addInsightPrompt() {
                const text = await showDialog({
                    title: "Add Memory",
                    input: true,
                    placeholder: 'e.g., "My name is Alex"',
                    confirmText: "Add",
                });
                if (!text) return;
                try {
                    const resp = await apiFetch("/api/insights", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ content: text }),
                    });
                    if (!resp.ok)
                        console.error("add insight failed:", resp.status);
                } catch (e) {
                    console.error("add insight:", e);
                }
                loadMemoryPanel();
            }

            async function refineInsights(btn) {
                btn.disabled = true;
                btn.textContent = "Refining...";
                try {
                    const resp = await apiFetch("/api/insights/refine", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ model: modelSel.value }),
                    });
                    if (!resp.ok) {
                        const err = await resp.json().catch(() => ({}));
                        console.error(
                            "refine error:",
                            err.error || resp.status,
                        );
                    }
                    loadMemoryPanel();
                } catch (e) {
                    console.error("refine:", e);
                }
                btn.disabled = false;
                btn.textContent = "Refine";
            }

            async function toggleMemory(enabled) {
                try {
                    const resp = await apiFetch("/api/insights/settings", {
                        method: "PATCH",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            memory_enabled: enabled ? "true" : "false",
                        }),
                    });
                    if (!resp.ok) throw new Error(resp.status);
                } catch (e) {
                    console.error("toggle memory failed:", e);
                    const cb = $("s-memory");
                    if (cb) cb.checked = !enabled;
                }
            }

            // --- Incognito mode ---
            function toggleIncognito() {
                if (!incognitoMode) {
                    // Enter incognito: start a fresh ephemeral chat
                    if (activeId && !activeId.startsWith("incog_"))
                        distillChat(activeId);
                    incognitoMode = true;
                    incognitoHistory = [];
                    document.body.classList.add("incognito");
                    $("incognito-btn").classList.add("active");
                    activeId = "incog_" + Date.now().toString(36);
                    chatMeta[activeId] = {
                        id: activeId,
                        title: "Incognito",
                        model: modelSel.value,
                        response_id: null,
                        updated_at: Date.now() / 1000,
                        pinned: 0,
                        folder: "",
                        _incognito: true,
                    };
                    msgs.innerHTML = "";
                    renderList();
                    updateExportBtn();
                    $("share-btn").classList.add("hidden");
                    input.focus();
                } else {
                    exitIncognito();
                    activeId = null;
                    renderWelcome();
                    renderList();
                    updateExportBtn();
                    input.focus();
                }
            }
            function exitIncognito() {
                const oldId = activeId;
                incognitoMode = false;
                incognitoHistory = [];
                document.body.classList.remove("incognito");
                $("incognito-btn").classList.remove("active");
                if (oldId && chatMeta[oldId] && chatMeta[oldId]._incognito)
                    delete chatMeta[oldId];
            }

            // --- Chat CRUD ---
            async function newChat() {
                if (settingsOpen) closeSettings();
                // Exit incognito if active
                if (incognitoMode) {
                    toggleIncognito();
                    return;
                }
                // Distill insights from the chat we're leaving
                if (activeId) distillChat(activeId);
                const resp = await apiFetch("/api/chats", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        title: "New chat",
                        model: modelSel.value,
                    }),
                });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    throw new Error(err.error || "Failed to create chat");
                }
                const data = await resp.json();
                const id = data.id;
                chatMeta[id] = {
                    id,
                    title: "New chat",
                    model: modelSel.value,
                    response_id: null,
                    updated_at: Date.now() / 1000,
                    pinned: 0,
                    folder: "",
                };
                activeId = id;
                renderList();
                renderWelcome();
                if (window.innerWidth <= 768) closeSB();
                updateExportBtn();
                input.focus();
            }

            async function loadChat(id) {
                const prevId = activeId;
                const wasStreaming = sending;
                if (sending) stopStream();
                // Close settings if open
                if (settingsOpen) closeSettings();
                // Exit incognito if active
                if (incognitoMode) exitIncognito();
                // Distill insights from the chat we're leaving
                if (activeId && activeId !== id) distillChat(activeId);
                activeId = id;
                const meta = chatMeta[id];
                if (
                    meta?.model &&
                    [...modelSel.options].some((o) => o.value === meta.model)
                )
                    modelSel.value = meta.model;
                renderList();
                try {
                    const resp = await apiFetch(`/api/chats/${id}/messages`);
                    if (!resp.ok) throw new Error("Failed to load chat");
                    const messages = await resp.json();
                    renderMessages(messages);
                    // Estimate context usage for the gauge (use real token_count when available, heuristic fallback)
                    if (messages.length) {
                        let totalTokens = 0;
                        for (const m of messages) {
                            if (m.token_count) {
                                totalTokens += m.token_count;
                            } else {
                                const text =
                                    (m.content || "") +
                                    (typeof m.output === "string" ?
                                        m.output
                                    :   "");
                                if (text) {
                                    totalTokens += estimateTokens(text);
                                }
                            }
                        }
                        if (totalTokens > 0)
                            updateCtxGauge(
                                totalTokens,
                                parseInt($("s-ctx").value) || 16000,
                            );
                    }
                } catch (e) {
                    console.error("loadChat:", e);
                    msgs.innerHTML =
                        '<div style="padding:var(--sp-10) 1.5rem;text-align:center;color:var(--err-text);font-size:var(--text-base)">Failed to load chat. <button class="retry-btn" style="color:var(--accent);background:none;border:none;cursor:pointer;text-decoration:underline;font-size:var(--text-base)">Retry</button></div>';
                    msgs.querySelector(".retry-btn")?.addEventListener(
                        "click",
                        () => loadChat(id),
                    );
                }
                if (window.innerWidth <= 768) closeSB();
                // If we aborted a stream, the previous chat's response_id may have been
                // persisted server-side but never reached the client via chat.end.
                // Refresh after a short delay to give the server time to persist.
                if (wasStreaming && prevId && prevId !== id && chatMeta[prevId]) {
                    setTimeout(async () => {
                        try {
                            const listResp = await apiFetch("/api/chats");
                            if (listResp.ok) {
                                const chats = await listResp.json();
                                const prev = chats.find(c => c.id === prevId);
                                if (prev && chatMeta[prevId]) {
                                    chatMeta[prevId].response_id = prev.response_id;
                                }
                            }
                        } catch (e) {
                            console.error("Failed to restore response_id for chat", prevId, e);
                        }
                    }, 1000);
                }
                await loadChatSettings(id);
                await loadPinNavigator(id);
                updateExportBtn();
            }

            async function deleteChat(id) {
                if (
                    !(await showDialog({
                        title: "Delete Chat",
                        message: "Delete this chat?",
                        confirmText: "Delete",
                        danger: true,
                    }))
                )
                    return;
                try {
                    const r = await apiFetch(`/api/chats/${id}`, { method: "DELETE" });
                    if (!r.ok) throw new Error(`${r.status}`);
                } catch (e) {
                    addErr("Failed to delete chat.");
                    return;
                }
                delete chatMeta[id];
                if (activeId === id) {
                    activeId = null;
                    renderWelcome();
                    updateExportBtn();
                }
                renderList();
            }

            async function deleteAll() {
                if (
                    !(await showDialog({
                        title: "Delete All Chats",
                        message: "Delete ALL chats? This cannot be undone.",
                        confirmText: "Delete All",
                        danger: true,
                    }))
                )
                    return;
                try {
                    const results = await Promise.allSettled(
                        Object.keys(chatMeta).map((id) =>
                            apiFetch(`/api/chats/${id}`, { method: "DELETE" }),
                        ),
                    );
                    const failed = results.filter(
                        (r) => r.status === "rejected" || (r.value && !r.value.ok),
                    );
                    if (failed.length) addErr(`${failed.length} chat(s) could not be deleted.`);
                } catch (e) {
                    addErr("Failed to delete chats.");
                    return;
                }
                chatMeta = {};
                activeId = null;
                renderList();
                renderWelcome();
                updateExportBtn();
                closeSettings();
            }

            // --- Render ---
            function renderWelcome() {
                msgs.innerHTML =
                    '<div class="welcome"><img src="/lm-chat-logo.svg" alt="" style="width:3rem;height:3rem;opacity:.45;margin-bottom:var(--sp-7)"><h2>LM Chat</h2><p>Local AI, deeply integrated with LM Studio.<br>MCP tools, context management, and more.</p><div class="w-hint"><span>Type a message or use <kbd>/</kbd> for commands</span><span><kbd>Cmd+N</kbd> new chat &nbsp; <kbd>Cmd+,</kbd> settings</span></div></div>';
                renderStarters();
            }

            function renderStarters() {
                const el = $("starters");
                if (!el) return;
                const starters = getStarters();
                el.innerHTML = starters
                    .map((s, i) => {
                        const svg = STARTER_ICONS[s.icon];
                        return `<button class="starter-card" data-action="use-starter" data-idx="${i}"><span class="starter-icon">${svg || esc(s.icon || "💬")}</span><span class="starter-title">${esc(s.title)}</span></button>`;
                    })
                    .join("");
                el.querySelectorAll('[data-action="use-starter"]').forEach(btn =>
                    btn.addEventListener('click', () => useStarter(parseInt(btn.dataset.idx)))
                );
            }
            function hideStarters() {
                const el = $("starters");
                if (el) el.innerHTML = "";
            }

            function useStarter(i) {
                const s = getStarters()[i];
                if (!s) return;
                input.value = s.prompt;
                input.focus();
                input.selectionStart = input.selectionEnd = s.prompt.length;
                input.dispatchEvent(new Event("input"));
            }

            function renderStarterSettings() {
                const el = $("starters-list");
                if (!el) return;
                const list = getStarters();
                el.innerHTML = list
                    .map(
                        (s, i) =>
                            `<div style="display:flex;gap:var(--sp-3);margin-bottom:var(--sp-2);align-items:center" data-starter-row="${i}"><input data-field="icon" value="${esc(s.icon || "💬")}" style="width:2rem;text-align:center;background:var(--surface);border:1px solid var(--border);border-radius:0.25rem;padding:0.1875rem;font-size:var(--text-base)"><input data-field="title" value="${esc(s.title)}" placeholder="Title" style="width:4.375rem;background:var(--surface);border:1px solid var(--border);border-radius:0.25rem;padding:0.1875rem var(--sp-3);font-size:var(--text-xs);color:var(--text)"><input data-field="prompt" value="${esc(s.prompt)}" placeholder="Prompt text..." style="flex:1;background:var(--surface);border:1px solid var(--border);border-radius:0.25rem;padding:0.1875rem var(--sp-3);font-size:var(--text-xs);color:var(--text)"><button data-action="remove-starter" style="background:none;border:none;color:var(--faint);cursor:pointer;font-size:var(--text-base)">&times;</button></div>`,
                    )
                    .join("");
                el.querySelectorAll('[data-starter-row]').forEach(row => {
                    const idx = parseInt(row.dataset.starterRow);
                    row.querySelectorAll('input[data-field]').forEach(inp =>
                        inp.addEventListener('change', () => updateStarter(idx, inp.dataset.field, inp.value))
                    );
                    row.querySelector('[data-action="remove-starter"]')?.addEventListener('click', () => removeStarter(idx));
                });
            }

            function updateStarter(i, field, value) {
                const list = getStarters();
                list[i][field] = value;
                saveStarters(list);
                renderStarters();
            }
            function addStarter() {
                const list = getStarters();
                list.push({ title: "New", prompt: "", icon: "💬" });
                saveStarters(list);
                renderStarterSettings();
            }
            function removeStarter(i) {
                const list = getStarters();
                list.splice(i, 1);
                saveStarters(list);
                renderStarterSettings();
                renderStarters();
            }
            function resetStarters() {
                localStorage.removeItem("lsc-starters");
                renderStarterSettings();
                renderStarters();
            }

            function renderMessages(list) {
                msgs.innerHTML = "";
                if (!list.length) {
                    renderWelcome();
                    return;
                }
                hideStarters();
                list.forEach((m) => {
                    if (m.role === "user") addUser(m.content, m.id);
                    else if (m.role === "assistant") {
                        let c = m.content || "";
                        const tm = c.match(/<think>([\s\S]*?)<\/think>/);
                        if (tm) {
                            c = c
                                .replace(/<think>[\s\S]*?<\/think>/, "")
                                .trim();
                            // Wrap think + response in a group div so they stay together
                            const grp = document.createElement("div");
                            grp.className = "msg-group";
                            getMsgTarget().appendChild(grp);
                            // Add think block inside group
                            const uid =
                                "th" + Math.random().toString(36).slice(2, 8);
                            const td = document.createElement("div");
                            td.className = "m-think";
                            td.innerHTML = thinkHtml(uid, "Show thinking", esc(tm[1].trim()), false);
                            bindThinkToggle(td);
                            grp.appendChild(td);
                            // Add response inside group
                            if (c) {
                                const { text: cleanC } = extractFollowups(c);
                                const ad = document.createElement("div");
                                ad.className = "m-asst";
                                if (m.id) ad.dataset.msgId = m.id;
                                const bub = document.createElement("div");
                                bub.className = "bub";
                                bub.innerHTML = md(cleanC);
                                if (window.hljs) bub.querySelectorAll('pre code').forEach(b => window.hljs.highlightElement(b));
                                ad.appendChild(bub);
                                ad.appendChild(buildMsgRow({
                                    role: "assistant",
                                    msgId: m.id,
                                    feedback: m.feedback ?? null,
                                    isPinned: false,
                                }));
                                grp.appendChild(ad);
                            }
                        } else if (c) {
                            const { text: cleanC } = extractFollowups(c);
                            addAsst(cleanC, {
                                msgId: m.id,
                                feedback: m.feedback ?? null,
                                isPinned: false,
                            });
                        }
                    } else if (m.role === "tool")
                        addTool(m.name, m.args, m.output);
                    else if (m.role === "error") addErr(m.content);
                });
                addCopyButtons();
                addRegenButton();
                scroll.scrollTop = scroll.scrollHeight;
            }

            function getMsgTarget() {
                const m = chatMeta[activeId];
                return (m && m._subchatTarget) || msgs;
            }
            function addUser(t, msgId, attachments) {
                const d = document.createElement("div");
                d.className = "m-user";
                if (msgId) d.dataset.msgId = msgId;
                d.dataset.text = t;
                const bub = document.createElement("div");
                bub.className = "bub";
                bub.innerHTML = esc(t);
                d.appendChild(bub);
                if (attachments && attachments.length) {
                    const imgBox = document.createElement("div");
                    imgBox.style.cssText =
                        "display:flex;gap:var(--sp-3);margin-top:var(--sp-4);flex-wrap:wrap";
                    attachments
                        .filter((a) => a.type === "image")
                        .forEach((a) => {
                            const img = document.createElement("img");
                            img.src = a.data_url;
                            img.alt = a.name || "image";
                            img.style.cssText =
                                "max-height:7.5rem;border-radius:var(--r-sm);cursor:pointer";
                            img.onclick = () =>
                                window.open(a.data_url, "_blank");
                            imgBox.appendChild(img);
                        });
                    attachments
                        .filter((a) => a.type === "file")
                        .forEach((a) => {
                            const tag = document.createElement("span");
                            tag.style.cssText =
                                "display:inline-flex;align-items:center;gap:var(--sp-2);background:var(--surface);border:1px solid var(--border);border-radius:var(--r-sm);padding:var(--sp-2) var(--sp-4);font-size:var(--text-xs);color:var(--dim)";
                            tag.textContent = a.name;
                            imgBox.appendChild(tag);
                        });
                    if (imgBox.children.length) bub.appendChild(imgBox);
                }
                d.appendChild(buildMsgRow({ role: "user", msgId }));
                getMsgTarget().appendChild(d);
            }
            function addAsst(t, opts = {}) {
                // opts: { msgId, feedback, isPinned }
                const d = document.createElement("div");
                d.className = "m-asst";
                if (opts.msgId) d.dataset.msgId = opts.msgId;
                const bub = document.createElement("div");
                bub.className = "bub";
                bub.innerHTML = md(t);
                if (window.hljs) bub.querySelectorAll('pre code').forEach(b => window.hljs.highlightElement(b));
                d.appendChild(bub);
                d.appendChild(buildMsgRow({
                    role: "assistant",
                    msgId: opts.msgId,
                    feedback: opts.feedback,
                    isPinned: opts.isPinned
                }));
                getMsgTarget().appendChild(d);
                return d;
            }
            function toolLabel(raw) {
                if (!raw) return "Used a tool";
                const friendly = {
                    brave_web_search: "Searched the web",
                    brave_local_search: "Searched locally",
                    memory_store: "Stored a memory",
                    memory_retrieve: "Retrieved memories",
                    memory_search: "Searched memories",
                    sequential_thinking: "Reasoned step by step",
                    resolve_library_id: "Looked up docs",
                    get_library_docs: "Retrieved docs",
                    search_papers: "Searched papers",
                    search_arxiv: "Searched arXiv",
                    search_semantic_scholar: "Searched papers",
                    search_google_scholar: "Searched papers",
                };
                if (friendly[raw]) return friendly[raw];
                return "Used " + raw.replace(/_/g, " ");
            }
            function toolPreview(raw) {
                // Extract the interesting bit from tool args for inline display
                if (!raw) return "";
                try {
                    const j = JSON.parse(raw);
                    return (
                        j.query ||
                        j.q ||
                        j.search ||
                        j.text ||
                        j.thought ||
                        j.content ||
                        j.key ||
                        j.input ||
                        ""
                    );
                } catch (e) {
                    return raw.length > 80 ? raw.slice(0, 80) + "…" : raw;
                }
            }
            function addTool(name, args, out) {
                const d = document.createElement("div");
                d.className = "m-tool";
                const uid = "tl" + Math.random().toString(36).slice(2, 8);
                const label = toolLabel(name);
                const argsStr =
                    typeof args === "string" ? args : (
                        JSON.stringify(args || "")
                    );
                const preview = toolPreview(argsStr);
                let bodyContent = "";
                if (args)
                    bodyContent += `<div class="t-args">${esc(argsStr)}</div>`;
                if (out) {
                    const o = extractToolOutput(out);
                    if (o)
                        bodyContent += `<div class="t-out">${esc(o.length > 1000 ? o.slice(0, 1000) + "…" : o)}</div>`;
                }
                const hasBody = !!bodyContent;
                d.innerHTML =
                    `<span class="t-name" style="display:none">${esc(name || "tool")}</span><div class="t-toggle" role="button" tabindex="0" ${hasBody ? `data-action="toggle-tool" data-uid="${uid}"` : ""}>` +
                    `<span class="t-arrow"${hasBody ? "" : ' style="visibility:hidden"'}>&#9656;</span> ${esc(label)}${preview ? `<span class="t-preview">${esc(preview)}</span>` : ""}</div>${hasBody ? `<div class="t-body" id="${uid}">${bodyContent}</div>` : ""}`;
                if (hasBody) {
                    const toggle = d.querySelector('[data-action="toggle-tool"]');
                    toggle.addEventListener('click', function() {
                        const b = document.getElementById(this.dataset.uid);
                        const a = this.querySelector('.t-arrow');
                        b.classList.toggle('open');
                        a.classList.toggle('open');
                    });
                    toggle.addEventListener('keydown', function(event) {
                        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); this.click(); }
                    });
                }
                getMsgTarget().appendChild(d);
            }
            function addThink(t) {
                const uid = "th" + Math.random().toString(36).slice(2, 8);
                const d = document.createElement("div");
                d.className = "m-think";
                d.innerHTML = thinkHtml(uid, "Show thinking", esc(t), false);
                bindThinkToggle(d);
                getMsgTarget().appendChild(d);
            }
            function addErr(t) {
                const d = document.createElement("div");
                d.className = "m-err";
                d.innerHTML = `<div class="bub">${esc(t)}</div>`;
                getMsgTarget().appendChild(d);
            }
            function addErrRetry(t) {
                const d = document.createElement("div");
                d.className = "m-err";
                d.innerHTML = `<div class="bub">${esc(t)} <button data-action="retry-last" style="color:var(--accent);background:none;border:none;cursor:pointer;text-decoration:underline;font-size:inherit;margin-left:var(--sp-3)">Retry</button></div>`;
                d.querySelector('[data-action="retry-last"]').addEventListener('click', retryLast);
                getMsgTarget().appendChild(d);
            }
            function retryLast() {
                const userEls = [...msgs.querySelectorAll(".m-user")];
                if (!userEls.length) return;
                const lastText = userEls[userEls.length - 1].dataset.text;
                if (!lastText) return;
                while (
                    msgs.lastChild &&
                    !(msgs.lastChild.className || "").includes("m-user")
                )
                    msgs.lastChild.remove();
                if (
                    msgs.lastChild &&
                    (msgs.lastChild.className || "").includes("m-user")
                )
                    msgs.lastChild.remove();
                if (activeId && chatMeta[activeId])
                    chatMeta[activeId].response_id = null;
                resendText(lastText);
            }

            // --- Send ---
            const sendIcon =
                '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>';
            const stopIcon =
                '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';
            let abortCtrl = null;

            function updateSendBtn() {
                send.disabled =
                    !input.value.trim() &&
                    !pendingAttachments.length &&
                    !sending;
            }
            input.addEventListener("input", () => {
                requestAnimationFrame(() => {
                    input.style.height = "auto";
                    input.style.height =
                        Math.min(input.scrollHeight, 160) + "px";
                });
                updateSendBtn();
                updateSlashMenu();
                localStorage.setItem("lsc-draft", input.value);
            });
            input.addEventListener("keydown", (e) => {
                if (slashKeyNav(e)) return;
                if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    sendMsg();
                }
            });
            input.addEventListener("blur", (e) => {
                // Don't close slash menu if focus moved to the slash button or menu itself
                setTimeout(() => {
                    const active = document.activeElement;
                    const menu = $("slash-menu");
                    if (active && (active.id === "slash-btn" || menu.contains(active))) return;
                    menu.classList.remove("open");
                }, 200);
            });
            send.onclick = () => {
                if (sending) {
                    stopStream();
                } else {
                    sendMsg();
                }
            };

            // --- Attachments: drag & drop, paste, file picker ---
            const mainEl = $("main");
            mainEl.addEventListener("dragover", (e) => {
                e.preventDefault();
                mainEl.classList.add("drag-over");
            });
            mainEl.addEventListener("dragleave", (e) => {
                if (!mainEl.contains(e.relatedTarget))
                    mainEl.classList.remove("drag-over");
            });
            mainEl.addEventListener("drop", (e) => {
                e.preventDefault();
                mainEl.classList.remove("drag-over");
                handleFiles(e.dataTransfer.files);
            });

            document.addEventListener("paste", (e) => {
                if (sending) return;
                const items = e.clipboardData?.items;
                if (!items) return;
                const files = [];
                for (const item of items) {
                    if (item.kind === "file") {
                        const f = item.getAsFile();
                        if (f) files.push(f);
                    }
                }
                if (files.length) {
                    e.preventDefault();
                    handleFiles(files);
                }
            });

            function handleFiles(fileList) {
                for (const file of fileList) {
                    if (ALLOWED_IMAGE_TYPES.includes(file.type)) {
                        if (file.size > MAX_IMAGE_SIZE) {
                            addErr(
                                "Image too large: " +
                                    file.name +
                                    " (max 20 MB)",
                            );
                            continue;
                        }
                        const r = new FileReader();
                        r.onload = () => {
                            pendingAttachments.push({
                                type: "image",
                                data_url: r.result,
                                name: file.name,
                            });
                            renderAttachments();
                            updateSendBtn();
                        };
                        r.readAsDataURL(file);
                    } else if (
                        file.type.startsWith("text/") ||
                        /\.(txt|md|csv|json|xml|yaml|yml|py|js|ts|html|css|log|sh|sql|toml|ini|cfg|conf|rb|go|rs|java|c|cpp|h|hpp)$/i.test(
                            file.name,
                        )
                    ) {
                        if (file.size > 1024 * 1024) {
                            addErr(
                                "File too large: " +
                                    file.name +
                                    " (max 1 MB for text)",
                            );
                            continue;
                        }
                        const r = new FileReader();
                        r.onload = () => {
                            pendingAttachments.push({
                                type: "file",
                                content: r.result,
                                name: file.name,
                            });
                            renderAttachments();
                            updateSendBtn();
                        };
                        r.readAsText(file);
                    } else {
                        addErr("Unsupported file type: " + file.name);
                    }
                }
            }

            function renderAttachments() {
                const el = $("attachments");
                el.innerHTML = "";
                pendingAttachments.forEach((att, i) => {
                    const div = document.createElement("div");
                    div.className = "att";
                    if (att.type === "image") {
                        const img = document.createElement("img");
                        img.src = att.data_url;
                        img.alt = att.name;
                        const btn = document.createElement("button");
                        btn.className = "att-x";
                        btn.textContent = "\u00d7";
                        btn.onclick = () => removeAttachment(i);
                        div.appendChild(img);
                        div.appendChild(btn);
                    } else {
                        div.innerHTML =
                            '<div class="att-file"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg><span>' +
                            esc(att.name) +
                            '</span></div><button class="att-x" data-action="remove-attachment">&times;</button>';
                        div.querySelector('[data-action="remove-attachment"]').addEventListener('click', () => removeAttachment(i));
                    }
                    el.appendChild(div);
                });
            }

            function removeAttachment(i) {
                pendingAttachments.splice(i, 1);
                renderAttachments();
                updateSendBtn();
            }

            const FOLLOWUP_SUFFIX =
                '\n\nAfter your response, suggest 2-3 natural follow-up questions the user might ask. Format them on the LAST line of your response as: <!--followups:["question 1","question 2","question 3"]-->';

            function extractFollowups(text) {
                const match = text.match(
                    /<!--followups:?\s*(\[.*?\])\s*-->\s*$/s,
                );
                if (!match) return { text, followups: [] };
                try {
                    const followups = JSON.parse(match[1]);
                    if (
                        Array.isArray(followups) &&
                        followups.length &&
                        followups.every((q) => typeof q === "string")
                    )
                        return {
                            text: text.slice(0, match.index).trimEnd(),
                            followups,
                        };
                } catch {}
                return { text, followups: [] };
            }

            function renderFollowups(questions, msgEl) {
                document
                    .querySelectorAll(".followups")
                    .forEach((el) => el.remove());
                const container = document.createElement("div");
                container.className = "followups";
                questions.slice(0, 3).forEach((q) => {
                    const btn = document.createElement("button");
                    btn.className = "followup-chip";
                    btn.textContent = q;
                    btn.onclick = () => {
                        input.value = q;
                        container.remove();
                        sendMsg();
                    };
                    container.appendChild(btn);
                });
                msgEl.after(container);
                scroll.scrollTop = scroll.scrollHeight;
            }

            function buildChatBody(text, systemPrompt, attachments) {
                const integrations = [
                    ...MCPS.filter((s) => s.on).map((s) => s.id),
                    ...remoteMcps
                        .filter((s) => s.on)
                        .map((s) => ({
                            type: "ephemeral_mcp",
                            server_label: s.label,
                            server_url: s.url,
                        })),
                ];
                const meta = chatMeta[activeId] || {};
                const model = modelSel.value || cachedModels[0]?.id || "";
                let inputVal = text;
                if (attachments && attachments.length) {
                    const inputArr = [];
                    const textParts = [];
                    if (text.trim()) textParts.push(text);
                    attachments
                        .filter((a) => a.type === "file")
                        .forEach((a) => {
                            textParts.push(
                                '\n\n<file name="' +
                                    a.name.replace(/"/g, '\\"') +
                                    '">\n' +
                                    a.content +
                                    "\n</file>",
                            );
                        });
                    if (textParts.length)
                        inputArr.push({
                            type: "text",
                            text: textParts.join(""),
                        });
                    attachments
                        .filter((a) => a.type === "image")
                        .forEach((a) => {
                            inputArr.push({ type: "image", url: a.data_url });
                        });
                    inputVal = inputArr;
                }
                // In incognito mode, prepend conversation history so the
                // model has context without any server-side state.
                // History is plain text only, held in JS memory, never persisted.
                if (incognitoMode && incognitoHistory.length > 1) {
                    // Build context from all turns except the last (which is
                    // the current user message already in inputVal).
                    // Trim oldest turns to stay within char budget.
                    const prior = incognitoHistory.slice(0, -1);
                    let ctx = "";
                    for (let i = prior.length - 1; i >= 0; i--) {
                        const line = (prior[i].role === "user" ? "User" : "Assistant") + ": " + prior[i].content;
                        if (ctx.length + line.length + 2 > INCOGNITO_HISTORY_MAX_CHARS) break;
                        ctx = line + (ctx ? "\n\n" + ctx : "");
                    }
                    const prefix = "[Conversation history]\n" + ctx + "\n\n[Current message]\n";
                    if (typeof inputVal === "string") {
                        inputVal = prefix + inputVal;
                    } else if (Array.isArray(inputVal)) {
                        // Multimodal input array — prepend history as a text part
                        inputVal.unshift({ type: "text", text: prefix });
                    }
                }
                const body = { model, input: inputVal, integrations };
                if (incognitoMode) {
                    body.incognito = true;
                } else {
                    body.chat_id = activeId;
                }
                const followupsEnabled =
                    localStorage.getItem("lsc-followups") !== "off";
                if (systemPrompt) {
                    body.system_prompt =
                        followupsEnabled ?
                            systemPrompt + FOLLOWUP_SUFFIX
                        :   systemPrompt;
                }
                const reasoning = $("s-reasoning").value;
                if (reasoning !== "off") body.reasoning = reasoning;
                if (!incognitoMode && meta.response_id)
                    body.previous_response_id = meta.response_id;
                // Sampling params
                // Auto-temperature: override based on active preset mode
                const PRESET_TEMPS = {
                    coder: 0.1,
                    creative: 0.9,
                    research: 0.4,
                    analyst: 0.3,
                    architect: 0.2,
                };
                const activePreset = (chatMeta[activeId] || {})._activePreset;
                const temp =
                    activePreset && PRESET_TEMPS[activePreset] !== undefined ?
                        PRESET_TEMPS[activePreset]
                    :   parseFloat($("s-temp").value);
                if (!isNaN(temp)) body.temperature = temp;
                const topP = parseFloat($("s-top-p").value);
                if (!isNaN(topP)) body.top_p = topP;
                const topK = parseInt($("s-top-k").value);
                if (!isNaN(topK)) body.top_k = topK;
                const minP = parseFloat($("s-min-p").value);
                if (!isNaN(minP)) body.min_p = minP;
                const repPen = parseFloat($("s-repeat-pen").value);
                if (!isNaN(repPen) && repPen !== 1.0)
                    body.repeat_penalty = repPen;
                const presPen = parseFloat($("s-presence-pen").value);
                if (!isNaN(presPen) && presPen > 0)
                    body.presence_penalty = presPen;
                const maxTok = parseInt($("s-max-tokens").value);
                if (!isNaN(maxTok) && maxTok > 0)
                    body.max_output_tokens = maxTok;
                // context_length is a load-time parameter — sending it per-request
                // triggers JIT model reloads in LM Studio. Read-only from instance config.
                // Apply per-chat SC/CoVe overrides when set via the chat settings panel.
                if (chatSettingsCache) {
                    if (chatSettingsCache.sc_enabled != null) body.sc_enabled = chatSettingsCache.sc_enabled;
                    if (chatSettingsCache.cove_enabled != null) body.cove_enabled = chatSettingsCache.cove_enabled;
                }
                return body;
            }

            function setSendMode(streaming) {
                if (streaming) {
                    send.innerHTML = stopIcon;
                    send.classList.add("stop");
                    send.disabled = false;
                    send.setAttribute("aria-label", "Stop generation");
                } else {
                    send.innerHTML = sendIcon;
                    send.classList.remove("stop");
                    updateSendBtn();
                    send.setAttribute("aria-label", "Send message");
                }
            }

            function stopStream() {
                if (abortCtrl) {
                    abortCtrl.abort();
                    abortCtrl = null;
                }
            }

            async function sendMsg() {
                if (sending) return;
                const text = input.value.trim();
                if (!text && !pendingAttachments.length) return;
                localStorage.removeItem("lsc-draft");
                document
                    .querySelectorAll(".followups")
                    .forEach((el) => el.remove());
                const sentAttachments =
                    pendingAttachments.length ? [...pendingAttachments] : null;
                pendingAttachments = [];
                renderAttachments();
                sending = true;
                setSendMode(true);

                // Handle slash commands
                const slashCmd = parseSlashCmd(text);
                if (slashCmd && slashCmd.type === "help") {
                    cancelSend(sentAttachments);
                    if (!activeId) await newChat();
                    showSlashHelp();
                    return;
                }
                if (slashCmd && slashCmd.type === "compact") {
                    cancelSend(sentAttachments);
                    triggerCompact();
                    return;
                }

                const actualText = slashCmd ? slashCmd.rest : text;
                if (slashCmd && !actualText) {
                    cancelSend();
                    addErr("Please provide a query after the command.");
                    return;
                }
                if (!actualText && !sentAttachments) {
                    cancelSend();
                    return;
                }

                if (!activeId) {
                    if (incognitoMode) {
                        activeId = "incog_" + Date.now().toString(36);
                        chatMeta[activeId] = {
                            id: activeId,
                            title: "Incognito",
                            model: modelSel.value,
                            response_id: null,
                            updated_at: Date.now() / 1000,
                            pinned: 0,
                            folder: "",
                            _incognito: true,
                        };
                    } else {
                        try {
                            await newChat();
                        } catch (e) {
                            cancelSend();
                            addErr("Failed to create chat: " + e.message);
                            return;
                        }
                    }
                }

                const meta = chatMeta[activeId];
                const isFirstMsg =
                    meta.title === "New chat" || meta.title === "Incognito";
                if (isFirstMsg && !incognitoMode) {
                    // Set a temporary short title; auto-title will overwrite it
                    const titleText = actualText || "Image attachment";
                    meta.title =
                        titleText.slice(0, 50) +
                        (titleText.length > 50 ? "..." : "");
                    meta.model = modelSel.value;
                    apiFetch(`/api/chats/${activeId}/title`, {
                        method: "PATCH",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ title: meta.title }),
                    });
                    renderList();
                    // Fire background auto-title request
                    autoTitle(activeId, actualText || titleText);
                }

                input.value = "";
                input.style.height = "auto";
                $("cmd-badge").classList.remove("visible");

                // Slash commands: sticky until user sends a plain message (no slash prefix)
                if (slashCmd && PRESETS[slashCmd.preset]) {
                    meta._activePreset = slashCmd.preset;
                } else if (!slashCmd) {
                    delete meta._activePreset;
                }
                const presetKey = meta._activePreset;

                // Sub-chat breakout room for command modes
                const modeLabel =
                    slashCmd ? slashCmd.label
                    : presetKey && PRESETS[presetKey] ?
                        SLASH_MENU_ITEMS.find((s) => s.preset === presetKey)
                            ?.desc || presetKey
                    :   null;
                // Clear welcome screen and starters if present
                const welcomeEl = msgs.querySelector('.welcome');
                if (welcomeEl) { msgs.innerHTML = ''; hideStarters(); }

                if (modeLabel) {
                    const frame = document.createElement("div");
                    frame.className = "subchat-frame";
                    frame.innerHTML = `<div class="subchat-label">${esc(slashCmd ? modeLabel : "Continuing · " + modeLabel)}</div>`;
                    msgs.appendChild(frame);
                    meta._subchatTarget = frame;
                } else {
                    delete meta._subchatTarget;
                }

                addUser(actualText, null, sentAttachments);
                scroll.scrollTop = scroll.scrollHeight;
                $("thinking").classList.add("on");

                // Track user message for incognito context
                if (incognitoMode) {
                    incognitoHistory.push({ role: "user", content: actualText });
                    if (incognitoHistory.length > INCOGNITO_HISTORY_MAX)
                        incognitoHistory.splice(0, incognitoHistory.length - INCOGNITO_HISTORY_MAX);
                }

                const sys =
                    presetKey && PRESETS[presetKey] ?
                        expandVars(PRESETS[presetKey])
                    :   expandVars($("s-sys").value.trim());
                const body = buildChatBody(
                    actualText,
                    sys || undefined,
                    sentAttachments,
                );

                await streamRequest(body, meta);
            }

            async function streamRequest(body, meta) {
                abortCtrl = new AbortController();
                try {
                    let resp = await apiFetch("/api/chat/stream", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(body),
                        signal: abortCtrl.signal,
                    });
                    if (!resp.ok) {
                        const err = await resp
                            .json()
                            .catch(() => ({ error: "Stream failed" }));
                        const errMsg =
                            err.error?.message ||
                            err.error ||
                            JSON.stringify(err);
                        if (
                            body.reasoning &&
                            /does not support reasoning/i.test(errMsg)
                        ) {
                            delete body.reasoning;
                            resp = await apiFetch("/api/chat/stream", {
                                method: "POST",
                                headers: { "Content-Type": "application/json" },
                                body: JSON.stringify(body),
                                signal: abortCtrl.signal,
                            });
                            if (!resp.ok) {
                                const err2 = await resp
                                    .json()
                                    .catch(() => ({ error: "Stream failed" }));
                                addErr(
                                    err2.error?.message ||
                                        err2.error ||
                                        JSON.stringify(err2),
                                );
                                finishStream(meta);
                                return;
                            }
                        } else {
                            addErr(errMsg);
                            finishStream(meta);
                            return;
                        }
                    }
                    const ct = resp.headers.get("content-type") || "";
                    if (!ct.includes("text/event-stream")) {
                        const data = await resp.json();
                        handleSyncResponse(data, meta);
                        finishStream(meta);
                        return;
                    }
                    await readSSE(resp.body, meta);
                } catch (e) {
                    if (e.name !== "AbortError")
                        addErrRetry("Connection failed: " + e.message);
                }
                finishStream(meta);
            }

            function finishStream(meta) {
                meta.updated_at = Date.now() / 1000;
                sending = false;
                abortCtrl = null;
                if (streamMdTimer) {
                    cancelAnimationFrame(streamMdTimer);
                    streamMdTimer = null;
                }
                setSendMode(false);
                $("thinking").classList.remove("on");
                // Track assistant response for incognito context.
                // If stream produced no content (error/abort), pop the
                // orphaned user turn so it doesn't confuse future context.
                if (incognitoMode) {
                    if (streamState.content) {
                        incognitoHistory.push({ role: "assistant", content: streamState.content });
                        if (incognitoHistory.length > INCOGNITO_HISTORY_MAX)
                            incognitoHistory.splice(0, incognitoHistory.length - INCOGNITO_HISTORY_MAX);
                    } else if (incognitoHistory.length && incognitoHistory[incognitoHistory.length - 1].role === "user") {
                        incognitoHistory.pop();
                    }
                }
                // Final markdown render
                const asstWrap =
                    streamState.bub ? streamState.bub.closest(".m-asst") : null;
                if (streamState.bub) {
                    streamState.bub.classList.remove("streaming");
                    const { text: cleanText, followups } =
                        extractFollowups(streamState.content);
                    if (followups.length) {
                        streamState.content = cleanText;
                        streamState.bub.innerHTML = md(cleanText);
                    } else {
                        streamState.bub.innerHTML = md(streamState.content);
                    }
                    if (window.hljs) streamState.bub.querySelectorAll('pre code').forEach(b => window.hljs.highlightElement(b));
                    streamState.bub = null;
                    streamState.content = "";
                    if (followups.length)
                        renderFollowups(followups, streamState.group || asstWrap);
                }
                // Append bottom row to just-completed response.
                // asstWrap was captured before streamState.bub was nullified — safe to use here.
                if (asstWrap && !asstWrap.querySelector(".msg-row")) {
                    asstWrap.appendChild(buildMsgRow({ role: "assistant" }));
                }
                // Add token stats after the assistant message
                if (streamState.deltaCount > 0) addMsgStats(streamState.group || asstWrap);
                addCopyButtons();
                addRegenButton();
                patchMsgIds();
                userScrolledUp = false;
                hideScrollBtn();
                scroll.scrollTop = scroll.scrollHeight;
                input.focus();
            }

            const streamState = {
                bub: null,
                content: "",
                inThink: false,
                thinkBuf: "",
                thinkBody: null,
                inReasoning: false,
                group: null,
                startTime: 0,
                firstTokenTime: 0,
                deltaCount: 0,
                endStats: null,
                liveStatsEl: null,
                deltaTimestamps: [],
            };
            function resetStreamState() {
                streamState.bub = null;
                streamState.content = "";
                streamState.inThink = false;
                streamState.thinkBuf = "";
                streamState.thinkBody = null;
                streamState.inReasoning = false;
                streamState.group = null;
                streamState.startTime = 0;
                streamState.firstTokenTime = 0;
                streamState.deltaCount = 0;
                streamState.endStats = null;
                streamState.liveStatsEl = null;
                streamState.deltaTimestamps = [];
            }

            async function readSSE(body, meta) {
                const reader = body.getReader();
                const dec = new TextDecoder();
                let buf = "";
                resetStreamState();
                streamState.startTime = performance.now();

                try {
                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;
                        buf += dec.decode(value, { stream: true });

                        // Process complete SSE blocks (separated by double newline)
                        let idx;
                        while ((idx = buf.indexOf("\n\n")) !== -1) {
                            const block = buf.slice(0, idx);
                            buf = buf.slice(idx + 2);
                            processSSEBlock(block, meta);
                        }
                    }
                    // Process any remaining data
                    if (buf.trim()) processSSEBlock(buf, meta);
                } catch (e) {
                    if (e.name !== "AbortError") throw e;
                }
            }

            function processSSEBlock(block, meta) {
                let event = "",
                    data = "";
                for (const line of block.split("\n")) {
                    if (line.startsWith("event:")) event = line.slice(6).trim();
                    else if (line.startsWith("data:"))
                        data += (data ? "\n" : "") + line.slice(5).trim();
                }
                if (!event) return;

                let parsed = {};
                if (data) {
                    try {
                        parsed = JSON.parse(data);
                    } catch (e) {}
                }

                switch (event) {
                    case "prompt_processing.start":
                        $("thinking").querySelector("span").textContent =
                            "Processing...";
                        break;
                    case "message.start":
                        $("thinking").querySelector("span").textContent =
                            "Generating...";
                        break;
                    case "reasoning.start":
                    case "reasoning.delta": {
                        if (!streamState.inReasoning) {
                            streamState.inReasoning = true;
                            // Create wrapper group for reasoning + response if not yet created
                            if (!streamState.group) {
                                streamState.group = document.createElement("div");
                                streamState.group.className = "msg-group";
                                getMsgTarget().appendChild(streamState.group);
                            }
                            const uid =
                                "th" + Math.random().toString(36).slice(2, 8);
                            const d = document.createElement("div");
                            d.className = "m-think";
                            d.innerHTML = thinkHtml(uid, "Thinking...", "", true);
                            bindThinkToggle(d);
                            streamState.group.appendChild(d);
                            streamState.thinkBody = d.querySelector(".think-body");
                            streamState.thinkBuf = "";
                        }
                        if (event === "reasoning.delta") {
                            streamState.thinkBuf += parsed.content || "";
                            if (streamState.thinkBody) streamState.thinkBody.textContent = streamState.thinkBuf;
                            autoScroll();
                        }
                        break;
                    }
                    case "reasoning.end":
                        if (streamState.inReasoning) {
                            streamState.inReasoning = false;
                            if (streamState.thinkBody) {
                                streamState.thinkBody.classList.remove("open");
                                streamState.thinkBody.parentElement.previousElementSibling.textContent =
                                    "Show thinking";
                            }
                        }
                        break;
                    case "error":
                        addErrRetry(
                            parsed.error?.message ||
                                parsed.error?.type ||
                                "Unknown streaming error",
                        );
                        $("thinking").classList.remove("on");
                        break;
                    case "message.delta": {
                        const delta = parsed.content || "";
                        if (!delta) break;

                        // Close reasoning block when content starts arriving
                        if (streamState.inReasoning) {
                            streamState.inReasoning = false;
                            if (streamState.thinkBody) {
                                streamState.thinkBody.classList.remove("open");
                                streamState.thinkBody.parentElement.previousElementSibling.textContent =
                                    "Show thinking";
                            }
                        }

                        // Track token stats
                        const now = performance.now();
                        streamState.deltaCount++;
                        if (!streamState.firstTokenTime) streamState.firstTokenTime = now;
                        streamState.deltaTimestamps.push(now);
                        // Keep rolling window of last 2 seconds
                        while (
                            streamState.deltaTimestamps.length > 1 &&
                            streamState.deltaTimestamps[0] < now - 2000
                        )
                            streamState.deltaTimestamps.shift();
                        // Update live stats display
                        if (streamState.deltaTimestamps.length > 1) {
                            const windowSec =
                                (streamState.deltaTimestamps[streamState.deltaTimestamps.length - 1] -
                                    streamState.deltaTimestamps[0]) /
                                1000;
                            if (windowSec > 0) {
                                const tps = (
                                    (streamState.deltaTimestamps.length - 1) / windowSec
                                ).toFixed(1);
                                updateLiveStats(tps + " tok/s");
                            }
                        }

                        // Handle <think> tags in streaming content
                        const combined = (streamState.inThink ? "" : streamState.content) + delta;
                        if (!streamState.inThink && !streamState.bub) {
                            // Check if content starts with <think>
                            if (combined.trimStart().startsWith("<think>")) {
                                streamState.inThink = true;
                                streamState.thinkBuf = combined.trimStart().slice(7);
                                // Create think element
                                const uid =
                                    "th" +
                                    Math.random().toString(36).slice(2, 8);
                                const d = document.createElement("div");
                                d.className = "m-think";
                                d.innerHTML = thinkHtml(uid, "Thinking...", "", true);
                                bindThinkToggle(d);
                                getMsgTarget().appendChild(d);
                                streamState.thinkBody = d.querySelector(".think-body");
                                streamState.thinkBody.textContent = streamState.thinkBuf;
                                autoScroll();
                                break;
                            }
                        }
                        if (streamState.inThink) {
                            streamState.thinkBuf += delta;
                            // Check for closing </think>
                            const closeIdx = streamState.thinkBuf.indexOf("</think>");
                            if (closeIdx !== -1) {
                                const thinkText = streamState.thinkBuf
                                    .slice(0, closeIdx)
                                    .trim();
                                if (streamState.thinkBody) {
                                    streamState.thinkBody.textContent = thinkText;
                                    streamState.thinkBody.classList.remove("open");
                                    streamState.thinkBody.parentElement.previousElementSibling.textContent =
                                        "Show thinking";
                                }
                                streamState.inThink = false;
                                // Remainder after </think> is normal content
                                const remainder = streamState.thinkBuf.slice(closeIdx + 8);
                                streamState.content = remainder;
                                if (remainder.trim()) {
                                    ensureStreamBub();
                                }
                            } else {
                                if (streamState.thinkBody) streamState.thinkBody.textContent = streamState.thinkBuf;
                            }
                            autoScroll();
                            break;
                        }

                        streamState.content += delta;
                        ensureStreamBub();
                        // Markdown rendering handled by streamMdTimer interval
                        autoScroll();
                        break;
                    }
                    case "tool_call.start":
                        $("thinking").querySelector("span").textContent =
                            `Using tools...`;
                        addToolStream(
                            parsed.id,
                            parsed.tool,
                            parsed.arguments || "",
                        );
                        break;
                    case "tool_call.arguments":
                        updateToolArgs(parsed.argumentsDelta || "");
                        break;
                    case "tool_call.success":
                        updateToolResult(parsed.id, parsed.output, true);
                        $("thinking").querySelector("span").textContent =
                            "Generating...";
                        break;
                    case "tool_call.failure":
                        updateToolResult(
                            parsed.id,
                            parsed.error || "Tool call failed",
                            false,
                        );
                        $("thinking").querySelector("span").textContent =
                            "Generating...";
                        break;
                    case "chat.end": {
                        // LM Studio native API nests response data under 'result'
                        const res = parsed.result || {};
                        const respId = parsed.response_id || res.response_id;
                        if (respId) meta.response_id = respId;
                        // Merge result stats into parsed for streamState.endStats consumers
                        const st = res.stats || {};
                        streamState.endStats = {
                            ...parsed,
                            stats: st,
                            usage: {
                                input_tokens: st.input_tokens || 0,
                                output_tokens: st.total_output_tokens || 0,
                            },
                        };
                        // Update context gauge
                        const inpTok = st.input_tokens || 0;
                        const ctxLen = parseInt($("s-ctx").value) || 16000;
                        if (inpTok) updateCtxGauge(inpTok, ctxLen);
                        $("thinking").classList.remove("on");
                        break;
                    }
                    case "usage":
                        // Dedicated usage event from server with real token counts
                        {
                            const uInp =
                                parsed.input_tokens ||
                                parsed.prompt_tokens ||
                                0;
                            const uCtx = parseInt($("s-ctx").value) || 16000;
                            if (uInp) updateCtxGauge(uInp, uCtx);
                        }
                        break;
                    case "status": {
                        const text = parsed.text || "";
                        const span = $("thinking")?.querySelector("span");
                        if (span) span.textContent = text;
                        break;
                    }
                }
            }

            function updateLiveStats(text) {
                if (!streamState.liveStatsEl) {
                    streamState.liveStatsEl = document.createElement("div");
                    streamState.liveStatsEl.className = "live-stats";
                    (streamState.group || getMsgTarget()).appendChild(streamState.liveStatsEl);
                }
                streamState.liveStatsEl.textContent = text;
            }

            function addMsgStats(container) {
                if (streamState.liveStatsEl) {
                    streamState.liveStatsEl.remove();
                    streamState.liveStatsEl = null;
                }
                const parts = [];
                const s = streamState.endStats || {};
                const stats = s.stats || {};
                const usage = s.usage || {};
                const tps = stats.tokens_per_second ||
                    (streamState.deltaCount > 2 && streamState.firstTokenTime ?
                        streamState.deltaCount / ((performance.now() - streamState.firstTokenTime) / 1000) : 0);
                if (tps > 0) parts.push(tps.toFixed(1) + " tok/s");
                const ttft = stats.time_to_first_token_seconds ||
                    (streamState.firstTokenTime && streamState.startTime ?
                        (streamState.firstTokenTime - streamState.startTime) / 1000 : 0);
                if (ttft > 0) parts.push("TTFT: " + ttft.toFixed(2) + "s");
                const inp = usage.input_tokens || usage.prompt_tokens || 0;
                const out = usage.output_tokens || usage.completion_tokens || streamState.deltaCount;
                if (inp || out) parts.push((inp || "?") + "\u2192" + out + " tokens");
                recordTokens(inp, out, tps);
                if (!parts.length) return;
                const statsText = parts.join(" \u00b7 ");
                const target = container || streamState.group || getMsgTarget();
                const statsEl = target?.querySelector(".msg-row-stats");
                if (statsEl) {
                    statsEl.textContent = statsText;
                }
                // All assistant messages include .msg-row-stats; no fallback path needed.
            }

            let streamMdTimer = null;
            function ensureStreamBub() {
                if (streamState.bub) return;
                const d = document.createElement("div");
                d.className = "m-asst";
                d.innerHTML = '<div class="bub streaming"></div>';
                // Append inside streamState.group if reasoning created one, otherwise directly to target
                (streamState.group || getMsgTarget()).appendChild(d);
                streamState.bub = d.querySelector(".bub");
                // Clear any orphaned timer before creating a new one
                if (streamMdTimer) cancelAnimationFrame(streamMdTimer);
                // Throttled markdown rendering during stream (~10fps)
                let lastRenderTime = 0;
                function streamRender() {
                    if (!streamState.bub || !streamState.content) return;
                    const now = performance.now();
                    if (now - lastRenderTime > 100) {
                        streamState.bub.innerHTML = md(streamState.content);
                        autoScroll();
                        lastRenderTime = now;
                    }
                    if (sending) streamMdTimer = requestAnimationFrame(streamRender);
                }
                streamMdTimer = requestAnimationFrame(streamRender);
            }

            let userScrolledUp = false,
                ignoreScrollEvent = false;
            scroll.addEventListener("scroll", () => {
                if (ignoreScrollEvent) return;
                const atBottom =
                    scroll.scrollHeight -
                        scroll.scrollTop -
                        scroll.clientHeight <
                    80;
                if (atBottom) {
                    userScrolledUp = false;
                    hideScrollBtn();
                } else {
                    userScrolledUp = sending;
                    showScrollBtn();
                }
            });
            function autoScroll() {
                if (userScrolledUp) return;
                scroll.scrollTop = scroll.scrollHeight;
            }
            function showScrollBtn() {
                const b = $("scroll-bottom");
                if (b) b.classList.add("visible");
            }
            function hideScrollBtn() {
                const b = $("scroll-bottom");
                if (b) b.classList.remove("visible");
            }
            function scrollToBottom() {
                userScrolledUp = false;
                scroll.scrollTop = scroll.scrollHeight;
                hideScrollBtn();
            }

            // Tool streaming state
            let curToolEl = null,
                curToolArgs = "";
            function addToolStream(id, name, args) {
                curToolArgs = args || "";
                const label = toolLabel(name);
                const uid = "tl" + Math.random().toString(36).slice(2, 8);
                const d = document.createElement("div");
                d.className = "m-tool";
                d.dataset.toolId = id || "";
                d.dataset.toolName = name || "";
                d.innerHTML = `<span class="t-name" style="display:none">${esc(name || "tool")}</span><div class="t-toggle" role="button" tabindex="0"><span class="t-arrow">&#9656;</span> ${esc(label)}<span class="t-preview"></span><span class="t-dots"><i></i><i></i><i></i></span></div><div class="t-body" id="${uid}"><div class="t-args"></div></div>`;
                // Append tool calls in order; text bubble will be re-appended after tools
                const target = streamState.group || getMsgTarget();
                ignoreScrollEvent = true;
                target.appendChild(d);
                // Move existing text bubble to end so it stays below tool calls
                const streamEl = streamState.bub && streamState.bub.closest(".m-asst");
                if (streamEl && streamEl.parentElement === target)
                    target.appendChild(streamEl);
                ignoreScrollEvent = false;
                curToolEl = d;
                if (args) {
                    curToolEl.querySelector(".t-args").textContent = args;
                    updateToolPreview();
                }
                autoScroll();
            }
            function updateToolPreview() {
                if (!curToolEl) return;
                const p = toolPreview(curToolArgs);
                const el = curToolEl.querySelector(".t-preview");
                if (el) el.textContent = p;
            }
            function updateToolArgs(delta) {
                if (!curToolEl) return;
                curToolArgs += delta;
                curToolEl.querySelector(".t-args").textContent = curToolArgs;
                updateToolPreview();
                autoScroll();
            }
            function extractToolOutput(output) {
                // MCP tools return [{type:"text",text:"..."}] — extract the text
                if (Array.isArray(output)) {
                    const texts = output
                        .filter((b) => b && b.type === "text" && b.text)
                        .map((b) => b.text);
                    if (texts.length) return texts.join("\n");
                }
                if (typeof output === "string") return output;
                if (output && typeof output === "object")
                    return JSON.stringify(output, null, 2);
                return "";
            }
            function updateToolResult(id, output, success) {
                if (!curToolEl) return;
                // Remove animated dots
                const dots = curToolEl.querySelector(".t-dots");
                if (dots) dots.remove();
                // Add result to body
                const o = extractToolOutput(output);
                const body = curToolEl.querySelector(".t-body");
                if (o && body) {
                    const out = document.createElement("div");
                    out.className = "t-out";
                    if (o.length > 1000) {
                        out.textContent = o.slice(0, 1000) + "…";
                        out.style.cursor = "pointer";
                        out.title = "Click to expand full output";
                        out.onclick = () => {
                            if (out.dataset.expanded) {
                                out.textContent = o.slice(0, 1000) + "…";
                                delete out.dataset.expanded;
                            } else {
                                out.textContent = o;
                                out.dataset.expanded = "1";
                            }
                        };
                    } else {
                        out.textContent = o;
                    }
                    body.appendChild(out);
                }
                // Make toggle clickable
                const toggle = curToolEl.querySelector(".t-toggle");
                const arrow = curToolEl.querySelector(".t-arrow");
                if (toggle && body) {
                    toggle.style.cursor = "pointer";
                    toggle.onclick = () => {
                        body.classList.toggle("open");
                        arrow.classList.toggle("open");
                    };
                    toggle.onkeydown = (e) => {
                        if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            toggle.click();
                        }
                    };
                }
                if (!success && toggle) toggle.style.color = "var(--err-text)";
                curToolEl = null;
                curToolArgs = "";
                autoScroll();
            }

            function handleSyncResponse(data, meta) {
                if (data.error) {
                    addErr(data.error.message || JSON.stringify(data.error));
                    return;
                }
                const syncRes = data.result || {};
                const syncRespId = data.response_id || syncRes.response_id;
                if (syncRespId) meta.response_id = syncRespId;
                if (Array.isArray(data.output))
                    data.output.forEach((item) => {
                        if (item.type === "tool_call")
                            addTool(item.tool, item.arguments, item.output);
                    });
                let content = extractContent(data);
                if (content) {
                    const tm = content.match(/<think>([\s\S]*?)<\/think>/);
                    if (tm) {
                        addThink(tm[1].trim());
                        content = content
                            .replace(/<think>[\s\S]*?<\/think>/, "")
                            .trim();
                    }
                    if (content) {
                        const { text: cleanText, followups } =
                            extractFollowups(content);
                        addAsst(cleanText);
                        if (followups.length) {
                            const lastAsst = getMsgTarget().querySelector(
                                ".m-asst:last-of-type",
                            );
                            if (lastAsst) renderFollowups(followups, lastAsst);
                        }
                    }
                }
                addCopyButtons();
            }

            function extractContent(d) {
                if (d.content) return d.content;
                if (d.output_text) return d.output_text;
                if (typeof d.output === "string") return d.output;
                if (Array.isArray(d.output))
                    return d.output
                        .filter((i) => i.type === "message")
                        .map((i) => i.content || "")
                        .join("\n");
                if (d.choices) return d.choices[0]?.message?.content || "";
                return "";
            }

            // --- Utils ---
            function thinkHtml(uid, label, content, startOpen) {
                const openClass = startOpen ? " open" : "";
                const style = startOpen ? ' style="display:block"' : "";
                return `<div class="think-toggle" role="button" tabindex="0" data-action="toggle-think" data-uid="${uid}">${label}</div><div class="bub"><div id="${uid}" class="think-body${openClass}"${style}>${content}</div></div>`;
            }
            function bindThinkToggle(container) {
                const toggle = container.querySelector('[data-action="toggle-think"]');
                if (!toggle) return;
                toggle.addEventListener('click', function() {
                    const b = document.getElementById(this.dataset.uid);
                    b.classList.toggle('open');
                    this.textContent = b.classList.contains('open') ? 'Hide thinking' : 'Show thinking';
                });
                toggle.addEventListener('keydown', function(event) {
                    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); this.click(); }
                });
            }
            function cancelSend(restoreAttachments) {
                sending = false;
                setSendMode(false);
                if (restoreAttachments) {
                    pendingAttachments = restoreAttachments;
                    renderAttachments();
                }
                input.value = "";
                input.style.height = "auto";
            }
            function estimateTokens(text) {
                const words = text.split(/\s+/).length;
                const special = (
                    text.match(/[{}\[\]()<>=/\\|@#$%^&*;:,.]/g) || []
                ).length;
                return Math.round(words * 1.3 + special * 0.5);
            }
            function esc(s) {
                return String(s)
                    .replace(/&/g, "&amp;")
                    .replace(/</g, "&lt;")
                    .replace(/>/g, "&gt;")
                    .replace(/"/g, "&quot;")
                    .replace(/'/g, "&#39;");
            }
            function _execCopy(text) {
                const ta = document.createElement('textarea');
                ta.value = text;
                ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;';
                document.body.appendChild(ta);
                ta.focus();
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
            }
            async function copyToClipboard(text) {
                try {
                    if (navigator.clipboard && navigator.clipboard.writeText) {
                        await navigator.clipboard.writeText(text);
                    } else {
                        _execCopy(text);
                    }
                } catch {
                    _execCopy(text);
                }
            }
            function showDialog(opts) {
                return new Promise((resolve) => {
                    const el = document.createElement("div");
                    el.className = "share-dialog";
                    el.setAttribute("role", "dialog");
                    el.setAttribute("aria-modal", "true");
                    const fields = (opts.fields || [])
                        .map(
                            (f, i) =>
                                `<div style="margin-top:var(--sp-5)"><label style="font-size:var(--text-sm);color:var(--dim);display:block;margin-bottom:var(--sp-2)">${esc(f.label)}</label><input id="dlg-f-${i}" type="${f.type || "text"}" placeholder="${esc(f.placeholder || "")}" value="${esc(f.value || "")}" style="width:100%;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-md);padding:var(--sp-4) var(--sp-5);font-size:var(--text-base);color:var(--text);font-family:inherit;outline:none;box-sizing:border-box" ${f.required ? "required" : ""}></div>`,
                        )
                        .join("");
                    const singleInput =
                        opts.input ?
                            `<div style="margin-top:var(--sp-5)"><input id="dlg-input" type="text" placeholder="${esc(opts.placeholder || "")}" value="${esc(opts.inputValue || "")}" style="width:100%;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-md);padding:var(--sp-4) var(--sp-5);font-size:var(--text-base);color:var(--text);font-family:inherit;outline:none;box-sizing:border-box"></div>`
                        :   "";
                    const dangerStyle =
                        opts.danger ?
                            "background:var(--err-bg,rgba(220,38,38,.15));color:var(--err-text);border:1px solid var(--err-border)"
                        :   "";
                    el.innerHTML = `<div class="share-content" style="animation:ddIn 150ms var(--ease)"><h3 style="margin:0 0 var(--sp-3)">${esc(opts.title)}</h3>${opts.message ? `<p style="color:var(--dim);font-size:var(--text-base);margin:var(--sp-2) 0 0">${esc(opts.message)}</p>` : ""}${singleInput}${fields}<div style="display:flex;gap:var(--sp-4);margin-top:var(--sp-7);justify-content:flex-end"><button id="dlg-cancel" class="sys-btn" style="background:var(--surface);border:1px solid var(--border);color:var(--dim)">${esc(opts.cancelText || "Cancel")}</button><button id="dlg-ok" class="sys-btn" style="${dangerStyle}">${esc(opts.confirmText || "OK")}</button></div></div>`;
                    document.body.appendChild(el);
                    const close = (val) => {
                        el.remove();
                        resolve(val);
                    };
                    el.querySelector("#dlg-cancel").onclick = () => close(null);
                    el.querySelector("#dlg-ok").onclick = () => {
                        if (opts.fields) {
                            const vals = opts.fields.map(
                                (_, i) => el.querySelector("#dlg-f-" + i).value,
                            );
                            close(vals);
                        } else if (opts.input)
                            close(el.querySelector("#dlg-input").value);
                        else close(true);
                    };
                    el.addEventListener("click", (e) => {
                        if (e.target === el) close(null);
                    });
                    el.addEventListener("keydown", (e) => {
                        if (e.key === "Escape") close(null);
                    });
                    const firstInput =
                        el.querySelector("input") ||
                        el.querySelector("#dlg-ok");
                    setTimeout(() => firstInput.focus(), 10);
                });
            }
            function sanitizeSvg(svg) {
                if (!svg) return "";
                // Parse in isolated document (no script execution) via DOMParser
                const doc = new DOMParser().parseFromString(
                    svg,
                    "image/svg+xml",
                );
                const el = doc.querySelector("svg");
                if (!el || doc.querySelector("parsererror")) return "";
                el.querySelectorAll(
                    "script,foreignObject,animate,set,style,use[href]",
                ).forEach((e) => e.remove());
                el.querySelectorAll("*").forEach((node) => {
                    [...node.attributes].forEach((attr) => {
                        if (
                            attr.name.startsWith("on") ||
                            attr.name === "style" ||
                            (attr.name === "href" && node.tagName !== "use")
                        )
                            node.removeAttribute(attr.name);
                    });
                });
                return el.outerHTML;
            }
            function md(text) {
                // Strip followup comments before rendering (extracted separately in finishStream).
                // First: complete comment. Second: partial comment still being emitted mid-stream.
                text = text.replace(/<!--followups:?\s*\[.*?\]\s*-->\s*$/s, "");
                text = text.replace(/<!--followups:?[\s\S]*$/s, "");
                let h = esc(text);
                h = h.replace(
                    /```(\w*)\n([\s\S]*?)```/g,
                    (_, lang, code) =>
                        `<pre${lang ? ` data-lang="${lang}"` : ""}><code>${code}</code></pre>`,
                );
                h = h.replace(/`([^`]+)`/g, "<code>$1</code>");
                h = h.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
                h = h.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, "<em>$1</em>");
                h = h.replace(/^### (.+)$/gm, "<h3>$1</h3>");
                h = h.replace(/^## (.+)$/gm, "<h2>$1</h2>");
                h = h.replace(/^# (.+)$/gm, "<h1>$1</h1>");
                h = h.replace(/^\&gt; (.+)$/gm, "<blockquote>$1</blockquote>");
                h = h.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m, text, url) => {
                    const raw = url
                        .replace(/&amp;/g, "&")
                        .replace(/&lt;/g, "<")
                        .replace(/&gt;/g, ">")
                        .replace(/&quot;/g, '"')
                        .replace(/&#39;/g, "'");
                    return /^https?:\/\//i.test(raw) || /^mailto:/i.test(raw) ?
                            `<a href="${url.replace(/"/g, "&quot;")}" target="_blank" rel="noopener">${text}</a>`
                        :   text;
                });
                // GFM pipe tables
                h = h.replace(
                    /((?:^|\n)\|.+\|[ ]*\n\|[\s:|-]+\|[ ]*\n(?:\|.+\|[ ]*\n?)+)/g,
                    (tbl) => {
                        const rows = tbl
                            .trim()
                            .split("\n")
                            .filter((r) => r.trim());
                        if (rows.length < 2) return tbl;
                        const parseRow = (r) =>
                            r
                                .replace(/^\|/, "")
                                .replace(/\|$/, "")
                                .split("|")
                                .map((c) => c.trim());
                        const hdr = parseRow(rows[0]);
                        const body = rows.slice(2).map(parseRow);
                        return (
                            "</p><table><thead><tr>" +
                            hdr.map((c) => "<th>" + c + "</th>").join("") +
                            "</tr></thead><tbody>" +
                            body
                                .map(
                                    (r) =>
                                        "<tr>" +
                                        r
                                            .map(
                                                (c) =>
                                                    "<td>" + c + "</td>",
                                            )
                                            .join("") +
                                        "</tr>",
                                )
                                .join("") +
                            "</tbody></table><p>"
                        );
                    },
                );
                h = h.replace(/\n\n/g, "</p><p>");
                h = h.replace(/\n/g, "<br>");
                return "<p>" + h + "</p>";
            }

            // --- Copy buttons on code blocks ---
            function addCopyButtons() {
                msgs.querySelectorAll("pre:not(.has-copy)").forEach((pre) => {
                    pre.classList.add("has-copy");
                    const btn = document.createElement("button");
                    btn.className = "copy-btn";
                    btn.textContent = "Copy";
                    btn.onclick = async () => {
                        const code = pre.querySelector("code");
                        await copyToClipboard(code ? code.textContent : pre.textContent);
                        btn.textContent = "Copied!";
                        setTimeout(() => (btn.textContent = "Copy"), 1500);
                    };
                    pre.appendChild(btn);
                });
            }

            // --- Regenerate button ---
            function addRegenButton() {
                // Clean up legacy standalone regen buttons (pre-refactor)
                msgs.querySelectorAll(".regen-wrap").forEach((el) => el.remove());
                if (sending || !activeId) return;
                const assts = msgs.querySelectorAll(".m-asst");
                if (!assts.length) return;
                const lastAsst = assts[assts.length - 1];
                const regenBtn = lastAsst.querySelector(".regen-btn");
                if (regenBtn) {
                    regenBtn.disabled = false;
                }
            }

            async function patchMsgIds() {
                if (!activeId) return;
                try {
                    const resp = await apiFetch(
                        `/api/chats/${activeId}/messages`,
                    );
                    const list = await resp.json();
                    const userMsgs = [...msgs.querySelectorAll(".m-user")];
                    const dbUsers = list.filter((m) => m.role === "user");
                    userMsgs.forEach((el, i) => {
                        if (dbUsers[i] && dbUsers[i].id)
                            el.dataset.msgId = dbUsers[i].id;
                    });
                    // Also patch assistant messages for forking
                    const asstMsgs = [...msgs.querySelectorAll(".m-asst")];
                    const dbAssts = list.filter((m) => m.role === "assistant");
                    asstMsgs.forEach((el, i) => {
                        if (dbAssts[i] && dbAssts[i].id)
                            el.dataset.msgId = dbAssts[i].id;
                    });
                } catch (e) {
                    console.error("patchMsgIds:", e);
                }
            }

            async function regenerate() {
                if (sending || !activeId) return;
                // Disable the regen button immediately (re-enabled by addRegenButton after stream completes)
                const assts = msgs.querySelectorAll(".m-asst");
                if (assts.length) {
                    const regenBtn = assts[assts.length - 1].querySelector(".regen-btn");
                    if (regenBtn) regenBtn.disabled = true;
                }
                // Incognito: re-send last user message from DOM (no server history)
                if (incognitoMode) {
                    const userEls = [...msgs.querySelectorAll(".m-user")];
                    if (!userEls.length) return;
                    const lastText = userEls[userEls.length - 1].dataset.text;
                    // Remove last assistant + tool + stats messages
                    while (msgs.lastChild) {
                        const cls = msgs.lastChild.className || "";
                        if (cls.includes("m-user")) break;
                        msgs.lastChild.remove();
                    }
                    if (
                        msgs.lastChild &&
                        (msgs.lastChild.className || "").includes("m-user")
                    )
                        msgs.lastChild.remove();
                    chatMeta[activeId].response_id = null;
                    await resendText(lastText);
                    return;
                }
                // Call server to delete last response
                let data;
                try {
                    const resp = await apiFetch(
                        `/api/chats/${activeId}/messages/last`,
                        { method: "DELETE" },
                    );
                    if (!resp.ok) { addErr("Failed to remove last message."); return; }
                    data = await resp.json();
                } catch (e) {
                    addErr("Failed to regenerate: " + e.message);
                    return;
                }
                if (!data.user_content) return;
                // Remove last assistant + tool + stats/indicator messages from DOM (walk backwards until user msg)
                while (msgs.lastChild) {
                    const cls = msgs.lastChild.className || "";
                    if (cls.includes("m-user")) break;
                    msgs.lastChild.remove();
                }
                // Also remove the last user message (server deleted it, resendText will re-add)
                if (
                    msgs.lastChild &&
                    (msgs.lastChild.className || "").includes("m-user")
                )
                    msgs.lastChild.remove();
                // Reset response_id since server cleared it
                chatMeta[activeId].response_id = null;
                // Resend the user message through streaming
                await resendText(data.user_content);
            }

            // --- Edit user message ---
            function startEdit(el) {
                if (sending) return;
                const text = el.dataset.text;
                const bub = el.querySelector(".bub");
                const editBtn = el.querySelector(".edit-btn");
                editBtn.style.display = "none";
                bub.style.display = "none";
                const area = document.createElement("div");
                area.className = "edit-area";
                area.innerHTML = `<textarea>${esc(text)}</textarea><div class="edit-btns"><button class="cancel" data-action="cancel-edit">Cancel</button><button class="save" data-action="save-edit">Save &amp; Send</button></div>`;
                area.querySelector('[data-action="cancel-edit"]').addEventListener('click', function() { cancelEdit(this); });
                area.querySelector('[data-action="save-edit"]').addEventListener('click', function() { saveEdit(this); });
                el.appendChild(area);
                const ta = area.querySelector("textarea");
                ta.style.height = Math.max(60, ta.scrollHeight) + "px";
                ta.focus();
            }

            function cancelEdit(btn) {
                const el = btn.closest(".m-user");
                el.querySelector(".edit-area").remove();
                el.querySelector(".bub").style.display = "";
                el.querySelector(".edit-btn").style.display = "";
            }

            async function saveEdit(btn) {
                if (sending) return;
                const el = btn.closest(".m-user");
                const newText = el
                    .querySelector(".edit-area textarea")
                    .value.trim();
                if (!newText) {
                    cancelEdit(btn);
                    return;
                }
                const msgId = el.dataset.msgId;
                // Truncate from this message onward in DB
                if (msgId && activeId) {
                    await apiFetch(`/api/chats/${activeId}/messages/truncate`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            from_message_id: parseInt(msgId),
                        }),
                    });
                    chatMeta[activeId].response_id = null;
                }
                // Remove this message and everything after it from DOM
                while (msgs.lastChild && msgs.lastChild !== el)
                    msgs.lastChild.remove();
                el.remove();
                // Resend edited text
                await resendText(newText);
            }

            async function resendText(text) {
                if (!activeId) return;
                const meta = chatMeta[activeId];
                addUser(text);
                scroll.scrollTop = scroll.scrollHeight;
                sending = true;
                setSendMode(true);
                $("thinking").classList.add("on");

                const presetKey = (chatMeta[activeId] || {})._activePreset;
                const sys =
                    presetKey && PRESETS[presetKey] ?
                        expandVars(PRESETS[presetKey])
                    :   expandVars($("s-sys").value.trim());
                const body = buildChatBody(text, sys || undefined);

                await streamRequest(body, meta);
            }

            // --- Model management ---
            let cachedModels = [];
            const ICON_VISION_PATH =
                "M10 4C5.6 4 2 7 .5 10c1.5 3 5.1 6 9.5 6s8-3 9.5-6c-1.5-3-5.1-6-9.5-6zm0 10a4 4 0 1 1 0-8 4 4 0 0 1 0 8zm0-6.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z";
            const ICON_TOOLS_PATH =
                "M17.4 4.2L14.5 7l-1.8-1.8L15.4 2A5.2 5.2 0 0 0 9 3c-.8.9-1.2 2-1.1 3.2L2.6 11.5a2 2 0 0 0 0 2.8l1.1 1.1a2 2 0 0 0 2.8 0l5.3-5.3c1.2.1 2.3-.3 3.2-1.1a5.2 5.2 0 0 0 1-6.4l-.1-.1zM5.5 14.2a.8.8 0 1 1 0-1.6.8.8 0 0 1 0 1.6z";
            function iconCopy() {
                return `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
            }
            function iconFork() {
                return `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>`;
            }
            function iconRegen() {
                return `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4.32"/></svg>`;
            }
            function iconThumbUp() {
                return `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3z"/><path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg>`;
            }
            function iconThumbDown() {
                return `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3z"/><path d="M17 2h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"/></svg>`;
            }
            function iconEdit() {
                return `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`;
            }
            function iconPin() {
                return `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="17" x2="12" y2="22"/><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"/></svg>`;
            }

            function buildMsgRow(opts) {
                // opts: { role, msgId, statsText, feedback, isPinned }
                const row = document.createElement("div");
                row.className = "msg-row";

                if (opts.role === "assistant") {
                    const stats = document.createElement("div");
                    stats.className = "msg-row-stats";
                    stats.textContent = opts.statsText || "";
                    row.appendChild(stats);

                    const actions = document.createElement("div");
                    actions.className = "msg-row-actions";

                    const copyBtn = document.createElement("button");
                    copyBtn.className = "msg-action-btn";
                    copyBtn.title = "Copy";
                    copyBtn.innerHTML = iconCopy();
                    copyBtn.onclick = () => {
                        const bub = copyBtn.closest(".m-asst, .msg-group")?.querySelector(".bub");
                        if (bub) copyToClipboard(bub.innerText || bub.textContent);
                    };
                    actions.appendChild(copyBtn);

                    const forkBtn = document.createElement("button");
                    forkBtn.className = "msg-action-btn fork-btn";
                    forkBtn.title = "Fork from here";
                    forkBtn.innerHTML = iconFork();
                    forkBtn.onclick = function() { forkFromMsg(this); };
                    actions.appendChild(forkBtn);

                    const regenBtn = document.createElement("button");
                    regenBtn.className = "msg-action-btn regen-btn";
                    regenBtn.title = "Regenerate";
                    regenBtn.innerHTML = iconRegen();
                    regenBtn.disabled = true;
                    regenBtn.onclick = () => regenerate();
                    actions.appendChild(regenBtn);

                    const sep = document.createElement("span");
                    sep.style.cssText = "width:1px;height:12px;background:var(--border);margin:0 2px";
                    actions.appendChild(sep);

                    const upBtn = document.createElement("button");
                    upBtn.className = "msg-action-btn thumb-up" + (opts.feedback === 1 ? " voted-up" : "");
                    upBtn.title = "Helpful";
                    upBtn.innerHTML = iconThumbUp();
                    upBtn.onclick = () => {
                        const msgId = upBtn.closest("[data-msg-id]")?.dataset.msgId;
                        if (!msgId) return;
                        submitFeedback(msgId, upBtn.classList.contains("voted-up") ? 0 : 1, row);
                    };
                    actions.appendChild(upBtn);

                    const downBtn = document.createElement("button");
                    downBtn.className = "msg-action-btn thumb-down" + (opts.feedback === -1 ? " voted-down" : "");
                    downBtn.title = "Not helpful";
                    downBtn.innerHTML = iconThumbDown();
                    downBtn.onclick = () => {
                        const msgId = downBtn.closest("[data-msg-id]")?.dataset.msgId;
                        if (!msgId) return;
                        submitFeedback(msgId, downBtn.classList.contains("voted-down") ? 0 : -1, row);
                    };
                    actions.appendChild(downBtn);

                    if (opts.isPinned) {
                        const pinBtn = document.createElement("button");
                        pinBtn.className = "msg-action-btn pin-active";
                        pinBtn.title = "Pinned — click to view navigator";
                        pinBtn.innerHTML = iconPin();
                        pinBtn.onclick = () => openPinNavigator();
                        actions.appendChild(pinBtn);
                    }

                    // Hover-only pin action (for unpinned messages)
                    if (!opts.isPinned) {
                        const hoverPinBtn = document.createElement("button");
                        hoverPinBtn.className = "msg-action-btn hover-pin-btn";
                        hoverPinBtn.title = "Pin this response";
                        hoverPinBtn.style.display = "none";
                        hoverPinBtn.innerHTML = iconPin();
                        hoverPinBtn.onclick = () => {
                            const msgId = hoverPinBtn.closest("[data-msg-id]")?.dataset.msgId;
                            if (!msgId) return;
                            pinMessage(msgId, hoverPinBtn.closest(".msg-row"));
                        };
                        actions.appendChild(hoverPinBtn);
                    }

                    row.appendChild(actions);
                } else if (opts.role === "user") {
                    const spacer = document.createElement("div");
                    spacer.style.flex = "1";
                    row.appendChild(spacer);

                    const actions = document.createElement("div");
                    actions.className = "msg-row-actions";

                    const copyBtn = document.createElement("button");
                    copyBtn.className = "msg-action-btn";
                    copyBtn.title = "Copy";
                    copyBtn.innerHTML = iconCopy();
                    copyBtn.onclick = () => {
                        const bub = copyBtn.closest(".m-user")?.querySelector(".bub");
                        if (bub) copyToClipboard(bub.innerText || bub.textContent);
                    };
                    actions.appendChild(copyBtn);

                    const forkBtn = document.createElement("button");
                    forkBtn.className = "msg-action-btn fork-btn";
                    forkBtn.title = "Fork from here";
                    forkBtn.innerHTML = iconFork();
                    forkBtn.onclick = function() { forkFromMsg(this); };
                    actions.appendChild(forkBtn);

                    const editBtn = document.createElement("button");
                    editBtn.className = "msg-action-btn edit-btn";
                    editBtn.title = "Edit";
                    editBtn.innerHTML = iconEdit();
                    editBtn.onclick = function() { startEdit(this.closest(".m-user")); };
                    actions.appendChild(editBtn);

                    row.appendChild(actions);
                }

                return row;
            }

            async function pinMessage(msgId, rowEl) {
                try {
                    const r = await apiFetch(`/api/messages/${msgId}/pin`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: "{}"
                    });
                    if (!r.ok) throw new Error("pin failed");
                    const data = await r.json();
                    const actions = rowEl?.querySelector(".msg-row-actions");
                    if (actions) {
                        actions.querySelector(".hover-pin-btn")?.remove();
                        const pinBtn = document.createElement("button");
                        pinBtn.className = "msg-action-btn pin-active";
                        pinBtn.title = "Pinned";
                        pinBtn.innerHTML = iconPin();
                        pinBtn.dataset.pinId = data.id;
                        pinBtn.onclick = () => openPinNavigator();
                        actions.appendChild(pinBtn);
                    }
                    await loadPinNavigator(activeId);
                } catch(e) {
                    console.error("Pin error:", e);
                }
            }

            async function unpinMessage(pinId, pinBtnEl) {
                try {
                    const r = await apiFetch(`/api/pins/${pinId}`, { method: "DELETE" });
                    if (!r.ok) throw new Error("unpin failed");
                    const actions = pinBtnEl?.closest(".msg-row-actions");
                    if (actions) {
                        // Read msgId before detaching — closest() returns null on detached nodes
                        const msgId = pinBtnEl.closest("[data-msg-id]")?.dataset.msgId;
                        pinBtnEl.remove();
                        if (msgId) {
                            const hoverBtn = document.createElement("button");
                            hoverBtn.className = "msg-action-btn hover-pin-btn";
                            hoverBtn.title = "Pin this response";
                            hoverBtn.style.display = "none";
                            hoverBtn.innerHTML = iconPin();
                            hoverBtn.onclick = () => pinMessage(msgId, hoverBtn.closest(".msg-row"));
                            actions.appendChild(hoverBtn);
                        }
                    }
                    await loadPinNavigator(activeId);
                } catch(e) {
                    console.error("Unpin error:", e);
                }
            }

            // Stubs — submitFeedback replaced here (CF-T8); openPinNavigator replaced in CF-T9
            async function submitFeedback(msgId, rating, rowEl) {
                const upBtn = rowEl?.querySelector(".thumb-up");
                const downBtn = rowEl?.querySelector(".thumb-down");
                const wasUp = upBtn?.classList.contains("voted-up") ?? false;
                const wasDown = downBtn?.classList.contains("voted-down") ?? false;
                if (upBtn) upBtn.classList.toggle("voted-up", rating === 1);
                if (downBtn) downBtn.classList.toggle("voted-down", rating === -1);
                try {
                    const r = await apiFetch(`/api/messages/${msgId}/feedback`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ rating })
                    });
                    if (!r.ok) throw new Error("feedback failed");
                } catch(e) {
                    if (upBtn) upBtn.classList.toggle("voted-up", wasUp);
                    if (downBtn) downBtn.classList.toggle("voted-down", wasDown);
                    console.error("Feedback error:", e);
                }
            }
            function openPinNavigator() {
                $("pin-nav")?.classList.remove("collapsed");
                $("pin-nav")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
            }

            // Right panel state: null | "settings" | "pins"
            let rightPanelState = null;

            function openRightPanel(mode) {
                if (rightPanelState === mode) {
                    closeRightPanel();
                    return;
                }
                rightPanelState = mode;
                $("right-panel").classList.add("open");
                $("right-panel-overlay").classList.add("open");
                if (mode === "settings") {
                    $("right-panel-title").textContent = "Chat Settings";
                    const actionBtn = $("right-panel-action-btn");
                    actionBtn.classList.remove("hidden");
                    actionBtn.textContent = "Reset";
                    actionBtn.onclick = resetChatSettings;
                    renderChatSettingsPanel();
                } else if (mode === "pins") {
                    $("right-panel-title").textContent = "Pinned";
                    $("right-panel-action-btn").classList.add("hidden");
                    renderGlobalPinsPanel();
                }
            }

            function closeRightPanel() {
                rightPanelState = null;
                $("right-panel").classList.remove("open");
                $("right-panel-overlay").classList.remove("open");
            }
            $("pins-btn")?.addEventListener("click", () => openRightPanel("pins"));

            // Chat settings: per-chat overrides state
            let chatSettingsCache = {};
            let chatSettingsDebounce = null;
            let chatSettingsPending = {};

            async function loadChatSettings(chatId, { refreshPanel = true } = {}) {
                const btn = $("chat-settings-btn");
                if (!chatId) {
                    chatSettingsCache = {};
                    if (btn) btn.classList.remove("has-overrides");
                    return;
                }
                try {
                    const r = await apiFetch(`/api/chats/${chatId}/settings`);
                    if (!r.ok) {
                        chatSettingsCache = {};
                        if (btn) btn.classList.remove("has-overrides");
                        return;
                    }
                    chatSettingsCache = await r.json();
                    if (btn) btn.classList.toggle("has-overrides", Object.keys(chatSettingsCache).length > 0);
                } catch(e) {
                    chatSettingsCache = {};
                    if (btn) btn.classList.remove("has-overrides");
                }
                // Re-render panel if open and caller requested it (skipped after saves to preserve focus)
                if (refreshPanel && rightPanelState === "settings") renderChatSettingsPanel();
            }

            function renderChatSettingsPanel() {
                const body = $("right-panel-body");
                const s = chatSettingsCache;
                body.innerHTML = `
                    <div class="sg">
                        <label>System Prompt</label>
                        <textarea id="cs-sys" rows="4" placeholder="(using global)"></textarea>
                    </div>
                    <div class="sg sg-row">
                        <div class="sg-half"><label>Temperature</label>
                            <input type="number" id="cs-temp" min="0" max="2" step="0.1" placeholder="(global)" value="${s.temperature ?? ""}"></div>
                        <div class="sg-half"><label>Top P</label>
                            <input type="number" id="cs-top-p" min="0" max="1" step="0.05" placeholder="(global)" value="${s.top_p ?? ""}"></div>
                    </div>
                    <div class="sg sg-row">
                        <div class="sg-half"><label>Top K</label>
                            <input type="number" id="cs-top-k" min="0" max="500" step="1" placeholder="(global)" value="${s.top_k ?? ""}"></div>
                        <div class="sg-half"><label>Min P</label>
                            <input type="number" id="cs-min-p" min="0" max="1" step="0.01" placeholder="(global)" value="${s.min_p ?? ""}"></div>
                    </div>
                    <div class="sg sg-row">
                        <div class="sg-half"><label>Repeat Penalty</label>
                            <input type="number" id="cs-repeat-pen" min="0" max="3" step="0.05" placeholder="(global)" value="${s.repeat_penalty ?? ""}"></div>
                        <div class="sg-half"><label>Presence Penalty</label>
                            <input type="number" id="cs-presence-pen" min="0" max="2" step="0.1" placeholder="(global)" value="${s.presence_penalty ?? ""}"></div>
                    </div>
                    <div class="sg"><label>Max Output Tokens</label>
                        <input type="number" id="cs-max-tokens" min="-1" max="32768" step="256" placeholder="(global)" value="${s.max_output_tokens ?? ""}">
                    </div>
                    <div class="sg"><label>Reasoning</label>
                        <select id="cs-reasoning">
                            <option value="">(global)</option>
                            <option value="off" ${s.reasoning==="off"?"selected":""}>Off</option>
                            <option value="medium" ${s.reasoning==="medium"?"selected":""}>Medium</option>
                            <option value="high" ${s.reasoning==="high"?"selected":""}>High</option>
                        </select>
                    </div>
                    <div class="sg" style="border-top:1px solid var(--border);padding-top:var(--sp-6);margin-top:var(--sp-6)">
                        <div class="toggle-row"><span>Self-Consistency</span>
                            <label class="sw"><input type="checkbox" id="cs-sc" ${s.sc_enabled?"checked":""}><span class="slider"></span></label>
                        </div>
                        <div class="toggle-row" style="margin-top:var(--sp-4)"><span>Chain of Verification</span>
                            <label class="sw"><input type="checkbox" id="cs-cove" ${s.cove_enabled?"checked":""}><span class="slider"></span></label>
                        </div>
                    </div>
                `;
                const ta = $("cs-sys");
                if (ta) ta.value = s.system_prompt || "";

                const fields = [
                    ["cs-sys",          "system_prompt",    (v) => v || null,               "textarea"],
                    ["cs-temp",         "temperature",      (v) => v===""?null:+v,          "input"],
                    ["cs-top-p",        "top_p",            (v) => v===""?null:+v,          "input"],
                    ["cs-top-k",        "top_k",            (v) => v===""?null:parseInt(v), "input"],
                    ["cs-min-p",        "min_p",            (v) => v===""?null:+v,          "input"],
                    ["cs-repeat-pen",   "repeat_penalty",   (v) => v===""?null:+v,          "input"],
                    ["cs-presence-pen", "presence_penalty", (v) => v===""?null:+v,          "input"],
                    ["cs-max-tokens",   "max_output_tokens",(v) => v===""?null:parseInt(v), "input"],
                    ["cs-reasoning",    "reasoning",        (v) => v||null,                 "select"],
                    ["cs-sc",           "sc_enabled",       (v) => v,                       "checkbox"],
                    ["cs-cove",         "cove_enabled",     (v) => v,                       "checkbox"],
                ];
                fields.forEach(([id, key, transform, type]) => {
                    const el = $(id);
                    if (!el) return;
                    el.addEventListener("change", () => {
                        const raw = type === "checkbox" ? el.checked : el.value;
                        saveChatSetting(key, transform(raw));
                    });
                    if (type !== "textarea") return;
                    el.addEventListener("input", () => {
                        saveChatSetting(key, transform(el.value));
                    });
                });
            }

            function saveChatSetting(key, value) {
                if (!activeId) return;
                chatSettingsPending[key] = value;
                if (chatSettingsDebounce) clearTimeout(chatSettingsDebounce);
                chatSettingsDebounce = setTimeout(async () => {
                    const payload = { ...chatSettingsPending };
                    chatSettingsPending = {};
                    try {
                        await apiFetch(`/api/chats/${activeId}/settings`, {
                            method: "PATCH",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify(payload)
                        });
                        await loadChatSettings(activeId, { refreshPanel: false });
                    } catch(e) {
                        console.error("Failed to save chat setting:", e);
                    }
                }, 400);
            }

            async function resetChatSettings() {
                if (!activeId) return;
                try {
                    await apiFetch(`/api/chats/${activeId}/settings`, { method: "DELETE" });
                    chatSettingsCache = {};
                    renderChatSettingsPanel();
                    const btn = $("chat-settings-btn");
                    if (btn) btn.classList.remove("has-overrides");
                } catch(e) {
                    console.error("Failed to reset chat settings:", e);
                }
            }

            async function renderGlobalPinsPanel() {
                const body = $("right-panel-body");
                body.innerHTML = '<div style="color:var(--dim);font-size:var(--text-sm)">Loading...</div>';
                try {
                    const r = await apiFetch("/api/pins");
                    if (!r.ok) throw new Error("Failed to load pins");
                    const pins = await r.json();
                    if (!pins.length) {
                        body.innerHTML = '<div style="color:var(--faint);font-size:var(--text-sm);text-align:center;padding:var(--sp-8)">No pins yet</div>';
                        return;
                    }
                    body.innerHTML = "";
                    pins.forEach(p => {
                        const item = document.createElement("div");
                        item.style.cssText = "padding:var(--sp-4) 0;border-bottom:1px solid var(--border)";
                        item.innerHTML = `
                            <div style="font-size:var(--text-sm);color:var(--text);margin-bottom:var(--sp-2)">${esc(p.pin_title || p.preview)}</div>
                            <div style="font-size:0.65rem;color:var(--faint)">${esc(p.chat_title)} · ${new Date(p.pinned_at * 1000).toLocaleDateString()}</div>
                        `;
                        const delBtn = document.createElement("button");
                        delBtn.className = "msg-action-btn";
                        delBtn.title = "Unpin";
                        delBtn.style.cssText = "float:right;color:var(--faint);margin-left:var(--sp-3)";
                        delBtn.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;
                        delBtn.onclick = (e) => {
                            e.stopPropagation();
                            apiFetch(`/api/pins/${p.id}`, { method: "DELETE" })
                                .then(() => {
                                    renderGlobalPinsPanel();
                                    if (activeId) loadPinNavigator(activeId);
                                })
                                .catch((err) => console.error("Failed to unpin:", err));
                        };
                        item.appendChild(delBtn);
                        item.style.cursor = "pointer";
                        item.onclick = async () => {
                            if (p.chat_id && p.message_id) {
                                await loadChat(p.chat_id);
                                scrollToMessage(p.message_id);
                                closeRightPanel();
                            } else {
                                const existing = item.querySelector(".pin-content-expanded");
                                if (!existing) {
                                    const expanded = document.createElement("div");
                                    expanded.className = "pin-content-expanded";
                                    expanded.style.cssText = "margin-top:var(--sp-3);font-size:var(--text-xs);color:var(--dim);white-space:pre-wrap;border-top:1px solid var(--border);padding-top:var(--sp-3)";
                                    expanded.textContent = p.preview || p.content || "(no content)";
                                    item.appendChild(expanded);
                                } else {
                                    existing.remove();
                                }
                            }
                        };
                        body.appendChild(item);
                    });
                } catch(e) {
                    body.innerHTML = '<div style="color:var(--faint);font-size:var(--text-sm)">Failed to load pins</div>';
                }
            }

            function togglePinNavigator() {
                $("pin-nav")?.classList.toggle("collapsed");
            }

            async function loadPinNavigator(chatId) {
                if (!chatId) return;
                try {
                    const r = await apiFetch(`/api/chats/${chatId}/pins`);
                    if (!r.ok) return;
                    const pins = await r.json();
                    const nav = $("pin-nav");
                    const list = $("pin-nav-list");
                    if (!pins.length) {
                        nav.classList.remove("has-pins");
                        return;
                    }
                    nav.classList.add("has-pins");
                    list.innerHTML = "";
                    pins.forEach(p => {
                        const item = document.createElement("div");
                        item.className = "pin-nav-item";
                        item.innerHTML = `
                            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <line x1="12" y1="17" x2="12" y2="22"/><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"/>
                            </svg>
                            <span>${esc(p.pin_title || "Pinned response")}</span>
                        `;
                        item.onclick = () => scrollToMessage(p.message_id);
                        list.appendChild(item);
                    });

                    // Reconcile isPinned indicators on already-rendered message rows
                    // Build both a set (for fast lookup) and a map (msgId → pinId for dataset)
                    const pinnedIds = new Set(pins.map(p => String(p.message_id)).filter(Boolean));
                    const pinIdByMsgId = new Map(pins.filter(p => p.message_id).map(p => [String(p.message_id), p.id]));

                    // Remove stale .pin-active buttons
                    msgs.querySelectorAll(".pin-active").forEach(btn => {
                        const id = btn.closest("[data-msg-id]")?.dataset.msgId;
                        if (!id || !pinnedIds.has(id)) btn.remove();
                    });

                    for (const id of pinnedIds) {
                        const msgEl = msgs.querySelector(`[data-msg-id="${id}"]`);
                        if (!msgEl) continue;
                        const actions = msgEl.querySelector(".msg-row-actions");
                        if (!actions || actions.querySelector(".pin-active")) continue;
                        actions.querySelector(".hover-pin-btn")?.remove();
                        const pinBtn = document.createElement("button");
                        pinBtn.className = "msg-action-btn pin-active";
                        pinBtn.title = "Pinned — click to view navigator";
                        pinBtn.innerHTML = iconPin();
                        pinBtn.dataset.pinId = pinIdByMsgId.get(id) ?? "";
                        pinBtn.onclick = () => openPinNavigator();
                        actions.appendChild(pinBtn);
                    }
                } catch(e) {
                    console.error("Load pin navigator:", e);
                }
            }

            function scrollToMessage(msgId) {
                if (!msgId) return;
                const el = msgs.querySelector(`[data-msg-id="${msgId}"]`);
                if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
            }

            function capIcon(path, fill) {
                return (
                    '<svg class="cap-icon" viewBox="0 0 20 20"><path fill="' +
                    fill +
                    '" d="' +
                    path +
                    '"/></svg>'
                );
            }

            function modelDisplayName(m) {
                return m.identifier || m.id;
            }
            function modelLabelHtml(m) {
                const caps = m.capabilities || {};
                let h = esc(modelDisplayName(m));
                if (caps.vision) h += capIcon(ICON_VISION_PATH, "var(--accent)");
                if (caps.trained_for_tool_use)
                    h += capIcon(ICON_TOOLS_PATH, "var(--green)");
                return h;
            }
            function modelLabel(m) {
                const caps = m.capabilities || {};
                let label = modelDisplayName(m);
                if (caps.vision) label += " ◉";
                if (caps.trained_for_tool_use) label += " ⚒";
                return label;
            }

            async function refreshModels() {
                try {
                    const r = await apiFetch("/api/models");
                    if (!r.ok) throw new Error("bad status");
                    const d = await r.json();
                    const raw = d.data || d.models || d || [];
                    // Normalize: LM Studio native API uses 'key' not 'id'
                    cachedModels = [];
                    raw.forEach((m) => {
                        const key = m.id || m.key || "";
                        if (!key || key.includes("embed") || key.includes("arena"))
                            return;
                        const inst = (m.loaded_instances || [])[0];
                        const instCfg = inst?.config || {};
                        const instCtx = instCfg.context_length || 0;
                        const maxCtx = m.max_context_length || 0;
                        // Use loaded instance id (user nickname) for API routing so
                        // LM Studio matches the already-loaded instance instead of
                        // JIT-loading a new one on every request.
                        const identifier = inst?.id || m.display_name || "";
                        const id = inst?.id || key;
                        // Use instance context if reasonable (>=1024), otherwise fall back to model max
                        cachedModels.push({
                            ...m,
                            id,
                            key,
                            identifier,
                            context_length: instCtx >= 1024 ? instCtx : maxCtx,
                            max_context_length: maxCtx,
                            instance_config: instCfg,
                        });
                    });
                    // Sort: loaded models first
                    cachedModels.sort((a, b) => {
                        const al =
                                (a.loaded_instances || []).length > 0 ? 0 : 1,
                            bl = (b.loaded_instances || []).length > 0 ? 0 : 1;
                        return al - bl;
                    });
                    renderModelList();
                    // Update topbar selector
                    const prev = modelSel.value;
                    modelSel.innerHTML = "";
                    cachedModels.forEach((m) => {
                        const o = document.createElement("option");
                        o.value = m.id;
                        o.textContent = modelLabel(m);
                        modelSel.appendChild(o);
                    });
                    const saved = localStorage.getItem("lsc-model");
                    // Find the currently loaded model in LM Studio
                    const loaded = cachedModels.find(
                        (m) => (m.loaded_instances || []).length > 0,
                    );
                    if (
                        saved &&
                        [...modelSel.options].some((o) => o.value === saved)
                    )
                        modelSel.value = saved;
                    else if (
                        loaded &&
                        [...modelSel.options].some((o) => o.value === loaded.id)
                    )
                        modelSel.value = loaded.id;
                    else if (
                        prev &&
                        [...modelSel.options].some((o) => o.value === prev)
                    )
                        modelSel.value = prev;
                    else if (modelSel.options.length)
                        modelSel.value = modelSel.options[0].value;
                    // Clear stale localStorage if saved model no longer exists
                    if (
                        saved &&
                        ![...modelSel.options].some((o) => o.value === saved)
                    )
                        localStorage.removeItem("lsc-model");
                    setConn(
                        "green",
                        cachedModels.length +
                            " model" +
                            (cachedModels.length !== 1 ? "s" : ""),
                    );
                    updateModelPill();
                    updateTopModelLabel();
                    syncModelSettings();
                } catch (e) {
                    setConn("red", "Disconnected");
                    renderModelList();
                    updateModelPill();
                    updateTopModelLabel();
                }
            }

            function renderModelList() {
                const list = $("model-list");
                if (!list) return;
                list.innerHTML = "";
                if (!cachedModels.length) {
                    list.innerHTML =
                        '<div class="model-empty">No models loaded</div>';
                    return;
                }
                cachedModels.forEach((m) => {
                    const isLoaded = (m.loaded_instances || []).length > 0;
                    const inst = (m.loaded_instances || [])[0];
                    const ctx = inst?.context_length || m.context_length || "";
                    const d = document.createElement("div");
                    d.className = "model-item" + (isLoaded ? "" : " unloaded");
                    d.innerHTML = `<span class="mi-name" title="${esc(m.key)}">${modelLabelHtml(m)}</span><span class="mi-status ${isLoaded ? "loaded" : "unloaded"}">${isLoaded ? "Loaded" : "Idle"}</span>${ctx ? `<span class="mi-ctx">${ctx}</span>` : ""}`;
                    list.appendChild(d);
                });
            }

            // --- Keyboard shortcuts ---
            function showKBShortcuts() {
                $("kb-modal").classList.add("open");
                $("kb-overlay").classList.add("open");
            }
            function hideKBShortcuts() {
                $("kb-modal").classList.remove("open");
                $("kb-overlay").classList.remove("open");
            }

            document.addEventListener("keydown", (e) => {
                const mod = e.metaKey || e.ctrlKey;
                const tag = e.target.tagName;
                const inInput =
                    tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";

                // Escape always works
                if (e.key === "Escape") {
                    e.preventDefault();
                    if ($("export-dd").classList.contains("open")) {
                        $("export-dd").classList.remove("open");
                        return;
                    }
                    if ($("kb-modal").classList.contains("open")) {
                        hideKBShortcuts();
                        return;
                    }
                    if (settingsOpen) {
                        closeSettings();
                        return;
                    }
                    // Cancel edit mode if active
                    const editArea = msgs.querySelector(".edit-area");
                    if (editArea) {
                        cancelEdit(editArea.querySelector(".cancel"));
                        return;
                    }
                    // Close sidebar on mobile
                    if (
                        !document.body.classList.contains("sb-closed") &&
                        window.innerWidth <= 768
                    ) {
                        closeSB();
                        return;
                    }
                    return;
                }

                // Don't fire other shortcuts when typing
                if (inInput) return;

                if (mod && e.key === "n") {
                    e.preventDefault();
                    newChat();
                    return;
                }
                if (mod && e.shiftKey && (e.key === "S" || e.key === "s")) {
                    e.preventDefault();
                    toggleSidebar();
                    return;
                }
                if (mod && e.key === ",") {
                    e.preventDefault();
                    if (settingsOpen) closeSettings();
                    else openSettings();
                    return;
                }
                if (mod && e.shiftKey && (e.key === "E" || e.key === "e")) {
                    e.preventDefault();
                    exportChat("md");
                    return;
                }
            });

            function toggleSidebar() {
                if (document.body.classList.contains("sb-closed")) openSB();
                else closeSB();
            }

            // --- Export chat ---
            function toggleExportDD() {
                $("export-dd").classList.toggle("open");
            }
            // Close export dropdown on outside click
            document.addEventListener("click", (e) => {
                if (!$("export-wrap").contains(e.target))
                    $("export-dd").classList.remove("open");
            });

            function updateExportBtn() {
                if (activeId) {
                    $("export-btn").classList.remove("hidden");
                    $("share-btn").classList.remove("hidden");
                } else {
                    $("export-btn").classList.add("hidden");
                    $("share-btn").classList.add("hidden");
                }
            }

            async function shareChat() {
                if (!activeId) return;
                let data;
                try {
                    const res = await apiFetch(
                        "/api/chats/" + activeId + "/share",
                        {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: "{}",
                        },
                    );
                    if (!res.ok) { addErr("Failed to create share link."); return; }
                    data = await res.json();
                } catch (e) {
                    addErr("Failed to create share link.");
                    return;
                }
                const url = location.origin + data.url;
                const dialog = document.createElement("div");
                dialog.className = "share-dialog";
                dialog.innerHTML =
                    '<div class="share-content"><h3>Share Conversation</h3><p style="color:var(--dim);font-size:var(--text-sm);margin:var(--sp-4) 0">Anyone with this link can view a read-only snapshot of this conversation.</p><div style="display:flex;gap:var(--sp-3)"><input id="share-url" value="' +
                    esc(url) +
                    '" readonly style="flex:1;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-sm);padding:var(--sp-3) var(--sp-5);font-size:var(--text-sm);color:var(--text);font-family:inherit"><button data-action="copy-share-url" class="sys-btn" style="white-space:nowrap">Copy</button></div><div style="display:flex;gap:var(--sp-4);margin-top:var(--sp-6);justify-content:flex-end"><button data-action="delete-share-link" data-chat-id="' +
                    activeId +
                    '" style="background:none;border:1px solid var(--err-border);color:var(--err-text);padding:var(--sp-2) var(--sp-6);border-radius:var(--r-sm);font-size:var(--text-xs);cursor:pointer;font-family:inherit">Delete Link</button><button data-action="close-share-dialog" class="sys-btn">Done</button></div></div>';
                dialog.querySelector('[data-action="copy-share-url"]').addEventListener('click', function() {
                    copyToClipboard(document.getElementById('share-url').value);
                    this.textContent = 'Copied!';
                    setTimeout(() => this.textContent = 'Copy', 1500);
                });
                dialog.querySelector('[data-action="delete-share-link"]').addEventListener('click', function() {
                    unshareChat(this.dataset.chatId, this.closest('.share-dialog'));
                });
                dialog.querySelector('[data-action="close-share-dialog"]').addEventListener('click', function() {
                    this.closest('.share-dialog').remove();
                });
                document.body.appendChild(dialog);
                // Close on backdrop click
                dialog.addEventListener("click", (e) => {
                    if (e.target === dialog) dialog.remove();
                });
            }

            async function unshareChat(chatId, dialog) {
                try {
                    const r = await apiFetch("/api/chats/" + chatId + "/share", {
                        method: "DELETE",
                    });
                    if (!r.ok) throw new Error(`${r.status}`);
                } catch (e) {
                    addErr("Failed to delete share link.");
                    return;
                }
                if (dialog) dialog.remove();
            }

            function slugify(s) {
                return (
                    s
                        .toLowerCase()
                        .replace(/[^a-z0-9]+/g, "-")
                        .replace(/^-|-$/g, "") || "chat"
                );
            }

            function exportChat(fmt) {
                $("export-dd").classList.remove("open");
                if (!activeId) return;
                const meta = chatMeta[activeId];
                const title = meta?.title || "Chat";
                const slug = slugify(title);

                // Gather messages from the DOM
                const items = [];
                msgs.querySelectorAll(
                    ".m-user,.m-asst,.m-tool,.m-think,.m-err",
                ).forEach((el) => {
                    if (el.classList.contains("m-user")) {
                        items.push({
                            role: "user",
                            content:
                                el.dataset.text ||
                                el.querySelector(".bub")?.textContent ||
                                "",
                        });
                    } else if (el.classList.contains("m-asst")) {
                        items.push({
                            role: "assistant",
                            content:
                                el.querySelector(".bub")?.textContent || "",
                        });
                    } else if (el.classList.contains("m-tool")) {
                        const name =
                            el.querySelector(".t-name")?.textContent || "";
                        const args =
                            el.querySelector(".t-args")?.textContent || "";
                        const out =
                            el.querySelector(".t-out")?.textContent || "";
                        items.push({ role: "tool", name, args, output: out });
                    } else if (el.classList.contains("m-think")) {
                        items.push({
                            role: "thinking",
                            content:
                                el.querySelector(".think-body")?.textContent ||
                                "",
                        });
                    } else if (el.classList.contains("m-err")) {
                        items.push({
                            role: "error",
                            content:
                                el.querySelector(".bub")?.textContent || "",
                        });
                    }
                });

                let blob, filename;
                if (fmt === "json") {
                    blob = new Blob(
                        [JSON.stringify({ title, messages: items }, null, 2)],
                        { type: "application/json" },
                    );
                    filename = slug + ".json";
                } else {
                    let lines = ["# " + title, ""];
                    items.forEach((m) => {
                        if (m.role === "user") {
                            lines.push("**You:** " + m.content, "");
                            lines.push("---", "");
                        } else if (m.role === "assistant") {
                            lines.push("**Assistant:** " + m.content, "");
                            lines.push("---", "");
                        } else if (m.role === "tool") {
                            lines.push(
                                "> **" +
                                    m.name +
                                    "**(" +
                                    m.args +
                                    ") → " +
                                    m.output,
                                "",
                            );
                        } else if (m.role === "thinking") {
                            lines.push("> *Thinking:* " + m.content, "");
                        } else if (m.role === "error") {
                            lines.push("> **Error:** " + m.content, "");
                        }
                    });
                    blob = new Blob([lines.join("\n")], {
                        type: "text/markdown",
                    });
                    filename = slug + ".md";
                }

                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = filename;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
            }

            // --- Slash Commands ---
            const SLASH_MENU_ITEMS = [
                {
                    cmd: "/research",
                    desc: "Deep Research mode",
                    preset: "research",
                },
                { cmd: "/code", desc: "Coding Agent mode", preset: "coder" },
                {
                    cmd: "/write",
                    desc: "Creative Writing mode",
                    preset: "creative",
                },
                {
                    cmd: "/analyze",
                    desc: "Strategic Analyst mode",
                    preset: "analyst",
                },
                {
                    cmd: "/architect",
                    desc: "Systems Architect mode",
                    preset: "architect",
                },
                { cmd: "/compact", desc: "Summarize and compact context" },
                { cmd: "/help", desc: "Show available commands" },
            ];
            const SLASH_CMDS = Object.fromEntries(
                SLASH_MENU_ITEMS.filter((s) => s.preset).map((s) => [
                    s.cmd.slice(1),
                    { preset: s.preset, label: s.desc },
                ]),
            );

            function parseSlashCmd(text) {
                if (!text.startsWith("/")) return null;
                const spaceIdx = text.indexOf(" ");
                const cmd =
                    spaceIdx > 0 ? text.slice(1, spaceIdx) : text.slice(1);
                const rest =
                    spaceIdx > 0 ? text.slice(spaceIdx + 1).trim() : "";
                if (cmd === "help") return { type: "help" };
                if (cmd === "compact") return { type: "compact" };
                if (SLASH_CMDS[cmd])
                    return {
                        type: "cmd",
                        cmd,
                        rest,
                        label: SLASH_CMDS[cmd].label,
                        preset: SLASH_CMDS[cmd].preset,
                    };
                return null;
            }

            function showSlashHelp() {
                const lines = [
                    "**Available Commands:**",
                    "",
                    "`/research <query>` — Deep Research mode",
                    "`/code <query>` — Coding Agent mode",
                    "`/write <query>` — Creative Writing mode",
                    "`/analyze <query>` — Strategic Analyst mode",
                    "`/architect <query>` — Systems Architect mode",
                    "`/compact` — Summarize and compact conversation context",
                    "`/help` — Show this help",
                    "",
                    "Commands temporarily switch the system prompt for one message.",
                ];
                addAsst(lines.join("\n"));
                addCopyButtons();
                scroll.scrollTop = scroll.scrollHeight;
            }

            // --- Slash Command Popup ---
            let slashIdx = -1;

            function updateSlashMenu() {
                const menu = $("slash-menu");
                const val = input.value;
                updateCmdBadge();
                if (!val.startsWith("/") || val.includes(" ")) {
                    menu.classList.remove("open");
                    slashIdx = -1;
                    return;
                }
                const filter = val.slice(1).toLowerCase();
                const matches = SLASH_MENU_ITEMS.filter((s) =>
                    s.cmd.slice(1).startsWith(filter),
                );
                if (!matches.length) {
                    menu.classList.remove("open");
                    slashIdx = -1;
                    return;
                }
                menu.innerHTML = "";
                matches.forEach((s, i) => {
                    const btn = document.createElement("button");
                    btn.innerHTML = `<span class="sc-cmd">${esc(s.cmd)}</span><span class="sc-desc">${esc(s.desc)}</span>`;
                    if (i === slashIdx) btn.classList.add("sel");
                    btn.onmousedown = (e) => {
                        e.preventDefault();
                        input.value = s.cmd + " ";
                        menu.classList.remove("open");
                        slashIdx = -1;
                        input.focus();
                        updateCmdBadge();
                    };
                    menu.appendChild(btn);
                });
                menu.classList.add("open");
            }

            function updateCmdBadge() {
                const badge = $("cmd-badge");
                const val = input.value;
                if (!val.startsWith("/")) {
                    badge.classList.remove("visible");
                    return;
                }
                const spaceIdx = val.indexOf(" ");
                if (spaceIdx < 0) {
                    badge.classList.remove("visible");
                    return;
                }
                const cmd = val.slice(1, spaceIdx).toLowerCase();
                const match = SLASH_MENU_ITEMS.find(
                    (s) => s.cmd.slice(1) === cmd,
                );
                if (!match) {
                    badge.classList.remove("visible");
                    return;
                }
                const name = match.cmd.slice(1);
                badge.textContent =
                    name.charAt(0).toUpperCase() + name.slice(1);
                badge.classList.add("visible");
            }

            function slashKeyNav(e) {
                const menu = $("slash-menu");
                if (!menu.classList.contains("open")) return false;
                const btns = menu.querySelectorAll("button");
                if (e.key === "ArrowDown") {
                    e.preventDefault();
                    slashIdx = Math.min(slashIdx + 1, btns.length - 1);
                    btns.forEach((b, i) =>
                        b.classList.toggle("sel", i === slashIdx),
                    );
                    return true;
                }
                if (e.key === "ArrowUp") {
                    e.preventDefault();
                    slashIdx = Math.max(slashIdx - 1, 0);
                    btns.forEach((b, i) =>
                        b.classList.toggle("sel", i === slashIdx),
                    );
                    return true;
                }
                if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
                    const target =
                        slashIdx >= 0 && btns[slashIdx] ?
                            btns[slashIdx]
                        :   btns[0];
                    if (target) {
                        e.preventDefault();
                        target.onmousedown(e);
                        return true;
                    }
                }
                if (e.key === "Escape") {
                    menu.classList.remove("open");
                    slashIdx = -1;
                    return true;
                }
                return false;
            }

            // --- Auto-Title via Model ---
            function autoTitle(chatId, userMessage) {
                const body = {
                    model: modelSel.value,
                    input:
                        "Title this conversation in 3-5 words. No quotes, no punctuation, just the title. The message: " +
                        userMessage,
                    temperature: 0.3,
                    integrations: [],
                    incognito: true,
                };
                apiFetch("/api/chat", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body),
                })
                    .then((r) => r.json())
                    .then((data) => {
                        let title = extractContent(data) || "";
                        title = title
                            .trim()
                            .replace(/^["']|["']$/g, "")
                            .replace(/\.$/, "")
                            .slice(0, 60);
                        if (!title || title.length < 2) return;
                        if (chatMeta[chatId]) {
                            chatMeta[chatId].title = title;
                            apiFetch(`/api/chats/${chatId}/title`, {
                                method: "PATCH",
                                headers: { "Content-Type": "application/json" },
                                body: JSON.stringify({ title }),
                            });
                            renderList();
                        }
                    })
                    .catch(() => {}); // silent fail — keep fallback title
            }

            // --- Context Window Gauge ---
            function updateCtxGauge(inputTokens, ctxLength) {
                if (!inputTokens || !ctxLength) return;
                const pct = Math.min((inputTokens / ctxLength) * 100, 100);
                const bar = $("ctx-bar");
                bar.style.width = pct + "%";
                // Color: accent(low) → amber(70%) → rose(85%)
                if (pct < 70) bar.style.background = "var(--accent)";
                else if (pct < 85)
                    bar.style.background =
                        "linear-gradient(90deg,var(--accent),var(--accent-hover))";
                else
                    bar.style.background =
                        "linear-gradient(90deg,var(--accent-hover),var(--gauge-warn))";
                $("ctx-label").textContent =
                    inputTokens.toLocaleString() +
                    " / " +
                    ctxLength.toLocaleString() +
                    " tokens (" +
                    Math.round(pct) +
                    "%)";
            }

            async function triggerCompact() {
                if (!activeId) {
                    addErr("No active conversation to compact.");
                    return;
                }
                const model = modelSel.value;
                addAsst("Compacting conversation...");
                try {
                    const resp = await apiFetch(
                        `/api/chats/${activeId}/compact`,
                        {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ model }),
                        },
                    );
                    if (!resp.ok) {
                        const text = await resp.text();
                        try {
                            const j = JSON.parse(text);
                            addErr(j.error || `Compact failed (${resp.status})`);
                        } catch {
                            addErr(`Compact failed (${resp.status})`);
                        }
                        return;
                    }
                    const data = await resp.json();
                    addAsst(
                        `**Context compacted.** Summarized ${data.messages_summarized} messages (deleted ${data.messages_deleted}).\n\n> ${data.summary}`,
                    );
                    addCopyButtons();
                    scroll.scrollTop = scroll.scrollHeight;
                } catch (e) {
                    addErr("Compact failed: " + e.message);
                }
            }

            // --- Conversation Forking ---
            async function forkFromMsg(el) {
                if (!activeId) return;
                // Find the message element
                const msgEl = el.closest(".m-user,.m-asst");
                if (!msgEl) return;
                const msgId = msgEl.dataset.msgId;
                if (!msgId) {
                    // If no msgId, need to find the last known msgId at or before this point
                    // Walk backwards through siblings to find one with a msgId
                    let node = msgEl;
                    let foundId = null;
                    while (node) {
                        if (node.dataset && node.dataset.msgId) {
                            foundId = node.dataset.msgId;
                            break;
                        }
                        node = node.previousElementSibling;
                    }
                    if (!foundId) {
                        addErr("Cannot fork: message not yet saved");
                        return;
                    }
                    await doFork(foundId);
                } else {
                    await doFork(msgId);
                }
            }

            async function doFork(upToMsgId) {
                try {
                    const meta = chatMeta[activeId];
                    const resp = await apiFetch(`/api/chats/${activeId}/fork`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            up_to_message_id: parseInt(upToMsgId),
                            model: modelSel.value,
                        }),
                    });
                    const data = await resp.json();
                    if (data.error) {
                        addErr(data.error);
                        return;
                    }
                    // Add to chatMeta and switch to it
                    chatMeta[data.id] = {
                        id: data.id,
                        title: data.title,
                        model: modelSel.value,
                        response_id: null,
                        updated_at: Date.now() / 1000,
                        pinned: 0,
                        folder: "",
                    };
                    renderList();
                    await loadChat(data.id);
                } catch (e) {
                    addErr("Fork failed: " + e.message);
                }
            }

            // --- Quick Model Pill ---
            function getModelShortName(id) {
                if (!id) return "?";
                const m = cachedModels.find((x) => x.id === id);
                if (m?.identifier) return m.identifier;
                const parts = id.split("/");
                return parts[parts.length - 1];
            }

            function updateModelPill() {
                const m = cachedModels.find((x) => x.id === modelSel.value);
                const pill = $("model-pill");
                if (m) {
                    const caps = m.capabilities || {};
                    let icons = "";
                    if (caps.vision)
                        icons += capIcon(ICON_VISION_PATH, "var(--accent)");
                    if (caps.trained_for_tool_use)
                        icons += capIcon(ICON_TOOLS_PATH, "var(--green)");
                    pill.innerHTML = esc(getModelShortName(m.id)) + icons;
                    pill.style.display = "";
                } else if (modelSel.value) {
                    pill.textContent = getModelShortName(modelSel.value);
                    pill.style.display = "";
                } else {
                    pill.style.display = "none";
                }
                pill.title =
                    modelSel.value ?
                        "Model: " + modelSel.value
                    :   "No model selected";
            }

            function renderModelDD(ddId) {
                const dd = $(ddId);
                if (!dd) return;
                if (dd.classList.contains("open")) {
                    dd.classList.remove("open");
                    return;
                }
                dd.innerHTML = "";
                if (!cachedModels.length) {
                    dd.innerHTML =
                        '<div class="mdd-empty">No models loaded</div>';
                    dd.classList.add("open");
                    return;
                }
                cachedModels.forEach((m) => {
                    const isLoaded = (m.loaded_instances || []).length > 0;
                    const caps = m.capabilities || {};
                    const item = document.createElement("button");
                    item.className =
                        "mdd-item" +
                        (m.id === modelSel.value ? " active" : "") +
                        (isLoaded ? "" : " unloaded");
                    let meta = "";
                    if (caps.vision)
                        meta +=
                            '<span class="mdd-cap"><svg viewBox="0 0 20 20"><path fill="currentColor" d="' +
                            ICON_VISION_PATH +
                            '"/></svg>Vision</span>';
                    if (caps.trained_for_tool_use)
                        meta +=
                            '<span class="mdd-cap"><svg viewBox="0 0 20 20"><path fill="currentColor" d="' +
                            ICON_TOOLS_PATH +
                            '"/></svg>Tools</span>';
                    if (isLoaded)
                        meta += '<span class="mdd-status loaded">Loaded</span>';
                    item.innerHTML =
                        '<span class="mdd-name">' +
                        esc(modelDisplayName(m)) +
                        "</span>" +
                        (meta ?
                            '<div class="mdd-meta">' + meta + "</div>"
                        :   "");
                    item.onclick = (e) => {
                        e.stopPropagation();
                        modelSel.value = m.id;
                        localStorage.setItem("lsc-model", m.id);
                        updateModelPill();
                        updateTopModelLabel();
                        syncModelSettings();
                        dd.classList.remove("open");
                    };
                    dd.appendChild(item);
                });
                dd.classList.add("open");
            }
            function toggleModelDD() {
                renderModelDD("model-dd");
            }
            function toggleTopModelDD() {
                renderModelDD("top-model-dd");
            }
            function updateTopModelLabel() {
                const m = cachedModels.find((x) => x.id === modelSel.value);
                $("model-sel-label").innerHTML =
                    m ?
                        modelLabelHtml(m)
                    :   esc(modelSel.value || "Select model");
            }
            // Close dropdowns on outside click
            document.addEventListener("click", (e) => {
                const mp = $("model-pill"),
                    md = $("model-dd"),
                    mw = $("model-sel-wrap"),
                    td = $("top-model-dd");
                if (mp && md && !mp.contains(e.target))
                    md.classList.remove("open");
                if (mw && td && !mw.contains(e.target))
                    td.classList.remove("open");
            });

            // --- Init ---
            async function initApp() {
                // Fetch models FIRST so the select is populated before loadChat sets its value
                await checkConnection();
                await loadChatList();
                const ids = Object.keys(chatMeta);
                if (ids.length > 0) {
                    const latest = ids.sort(
                        (a, b) =>
                            (chatMeta[b].updated_at || 0) -
                            (chatMeta[a].updated_at || 0),
                    )[0];
                    await loadChat(latest);
                } else {
                    renderWelcome();
                }
                await loadSettings(); // H4: load server-side API key status + remote MCP configs
                try {
                    const h = await apiFetch("/api/health");
                    const j = await h.json();
                    appVersion = j.version || "";
                } catch (e) {
                    console.error("Failed to fetch app version:", e);
                }
                const draft = localStorage.getItem("lsc-draft");
                if (draft) {
                    input.value = draft;
                    input.style.height = "auto";
                    input.style.height =
                        Math.min(input.scrollHeight, 160) + "px";
                    updateSendBtn();
                }
                updateSidebarStats();
                setInterval(checkConnection, 30000);
            }

            (async function boot() {
                const authed = await checkAuth();
                if (authed) await initApp();
            })();
            if ("serviceWorker" in navigator)
                navigator.serviceWorker.register("/sw.js").catch(() => {});

// --- Expose functions needed by dynamically-rendered HTML (innerHTML) ---
Object.assign(window, {
    doAuth, closeSB, openSB, newChat, closeSettings, openSettings,
    filterChats, clearChatSearch, toggleSearchMode, toggleIncognito,
    showKBShortcuts, hideKBShortcuts, toggleExportDD, exportChat,
    shareChat, scrollToBottom, useStarter, switchSettingsTab,
    applyPreset, toggleMemory, addInsightPrompt, refineInsights,
    clearInsights, deleteAll, saveServerSettings, clearApiKey,
    toggleDebugMode, addRemoteMcp, removeRemoteMcp, setMcpAuth,
    addStarter, updateStarter, removeStarter, resetStarters,
    saveProfile, doSettingsChangePassword, doSettingsInvite,
    doLogout, startTotpSetup, verifyTotpSetup, disableTotp,
    toggleModelDD, toggleTopModelDD, toggleUserDD, startEdit,
    saveEdit, cancelEdit, forkFromMsg, retryLast, regenerate,
    triggerCompact, handleFiles, removeAttachment, unshareChat,
    closeRightPanel, openRightPanel, togglePinNavigator,
    pinMessage, unpinMessage, loadPinNavigator, scrollToMessage,
});

function initEventHandlers() {
    // Auth
    document.getElementById('auth-btn')?.addEventListener('click', () => doAuth());
    document.getElementById('a-pass')?.addEventListener('keydown', e => { if (e.key === 'Enter') doAuth(); });

    // Sidebar
    document.getElementById('sb-overlay')?.addEventListener('click', () => closeSB());
    document.getElementById('new-chat')?.addEventListener('click', () => newChat());
    document.getElementById('close-sb')?.addEventListener('click', () => closeSB());
    document.getElementById('chat-search')?.addEventListener('input', () => filterChats());
    document.getElementById('chat-search-clear')?.addEventListener('click', () => clearChatSearch());
    document.getElementById('search-mode')?.addEventListener('click', () => toggleSearchMode());

    // Context gauge
    const ctxGauge = document.getElementById('ctx-gauge');
    ctxGauge?.addEventListener('click', () => triggerCompact());
    ctxGauge?.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); triggerCompact(); }
    });

    // Topbar
    document.getElementById('open-sb')?.addEventListener('click', () => openSB());
    const modelSelWrap = document.getElementById('model-sel-wrap');
    modelSelWrap?.addEventListener('click', () => toggleTopModelDD());
    modelSelWrap?.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleTopModelDD(); }
    });

    // Export
    document.getElementById('export-btn')?.addEventListener('click', () => toggleExportDD());
    document.querySelector('[data-action="export-md"]')?.addEventListener('click', () => exportChat('md'));
    document.querySelector('[data-action="export-json"]')?.addEventListener('click', () => exportChat('json'));

    // Topbar actions
    document.getElementById('incognito-btn')?.addEventListener('click', () => toggleIncognito());
    document.getElementById('share-btn')?.addEventListener('click', () => shareChat());
    document.querySelector('.tb[title="Keyboard shortcuts"]')?.addEventListener('click', () => showKBShortcuts());
    document.getElementById('chat-settings-btn')?.addEventListener('click', () => openRightPanel('settings'));
    document.getElementById('global-settings-btn')?.addEventListener('click', () => openSettings());

    // User avatar/dropdown
    const userAvatar = document.getElementById('user-avatar');
    userAvatar?.addEventListener('click', () => toggleUserDD());
    userAvatar?.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleUserDD(); }
    });
    // User DD buttons
    document.querySelector('#user-dd button:first-of-type')?.addEventListener('click', () => {
        document.getElementById('user-dd')?.classList.remove('open');
        openSettings();
    });
    document.querySelector('#user-dd button:last-child')?.addEventListener('click', () => doLogout());

    // Scroll
    document.getElementById('scroll-bottom')?.addEventListener('click', () => scrollToBottom());

    // Model pill in input area
    const modelPill = document.getElementById('model-pill');
    modelPill?.addEventListener('click', () => toggleModelDD());
    modelPill?.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleModelDD(); }
    });

    // File input
    document.querySelector('#attach-btn input[type="file"]')?.addEventListener('change', function() {
        handleFiles(this.files);
        this.value = '';
    });

    // Slash button
    document.getElementById('slash-btn')?.addEventListener('click', () => {
        const inp = document.getElementById('input');
        if (inp) {
            inp.value = '/';
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            inp.focus();
        }
    });

    // KB modal
    document.getElementById('kb-overlay')?.addEventListener('click', () => hideKBShortcuts());

    // Settings form
    document.getElementById('s-preset')?.addEventListener('change', () => applyPreset());
    document.getElementById('s-reasoning')?.addEventListener('change', function() {
        localStorage.setItem('lsc-reasoning', this.value);
    });
    document.getElementById('s-followups')?.addEventListener('change', function() {
        localStorage.setItem('lsc-followups', this.checked ? 'on' : 'off');
    });
    document.getElementById('s-memory')?.addEventListener('change', function() {
        toggleMemory(this.checked);
    });
    document.querySelector('.danger[data-action="delete-all"]')?.addEventListener('click', () => deleteAll());

    // Memory actions
    document.getElementById('btn-refine')?.addEventListener('click', function() { refineInsights(this); });
    document.querySelector('[data-action="add-insight"]')?.addEventListener('click', () => addInsightPrompt());
    document.querySelector('[data-action="clear-insights"]')?.addEventListener('click', () => clearInsights());

    // Starters
    document.querySelector('[data-action="add-starter"]')?.addEventListener('click', () => addStarter());
    document.querySelector('[data-action="reset-starters"]')?.addEventListener('click', () => resetStarters());

    // Right panel
    document.querySelector('#right-panel .tb[title="Close"]')?.addEventListener('click', () => closeRightPanel());
    document.getElementById('right-panel-overlay')?.addEventListener('click', () => closeRightPanel());
}

document.addEventListener('DOMContentLoaded', initEventHandlers);

// iOS PWA: safe-area-inset-bottom stays fixed when keyboard opens — toggle via JS.
if (navigator.standalone && window.visualViewport) {
    const inputArea = document.getElementById('input-area');
    const naturalHeight = () => window.innerWidth > window.innerHeight
        ? Math.min(screen.width, screen.height)
        : Math.max(screen.width, screen.height);
    const applySab = () => {
        const keyboardActive = window.visualViewport.height < naturalHeight() - 40;
        inputArea.style.paddingBottom = keyboardActive ? '0px' : 'env(safe-area-inset-bottom, 0px)';
    };
    window.visualViewport.addEventListener('resize', applySab);
    applySab();
}

