/* Game 6 - Digital City Rescue. No external JavaScript dependencies. */
(() => {
  "use strict";
  const role = document.body.dataset.role;
  if (!role || !["teacher", "team"].includes(role)) return;
  const API = "/api/thanh-pho";
  const ICONS = ["🚀", "⚡", "🛡️", "🤖", "🌟", "💎", "🔥", "🧠", "🛰️", "🎯", "🧩", "💻"];
  const LABELS = ["A", "B", "C", "D"];
  const PHASE_NAMES = {
    lobby: "ĐANG CHỜ CÁC ĐỘI", intro: "TRIỆU TẬP CÁC BIỆT ĐỘI",
    countdown: "CHUẨN BỊ CÂU HỎI", answering: "ĐANG NHẬN ĐÁP ÁN",
    review: "CÔNG BỐ ĐÁP ÁN", station: "KHÔI PHỤC TRẠM",
    finale_intro: "CHUNG KẾT BÍ MẬT", sealed: "KẾT QUẢ ĐÃ NIÊM PHONG",
    awards: "VINH DANH CÁC BIỆT ĐỘI", finished: "HOÀN THÀNH NHIỆM VỤ"
  };
  let room = (new URLSearchParams(location.search).get("room") || "").trim().toUpperCase();
  let token = "";
  let state = null;
  let clockOffset = 0;
  let inFlight = false;
  let previousTeacherKey = "";
  let previousTeamKey = "";
  let selectedChoice = null;
  let selectedBoost = false;
  let lastQuestionKey = "";
  let iconChosen = ICONS[0];
  let lastPhase = null;
  let soundOn = localStorage.getItem("city-game-sound") !== "off";
  let audioContext = null;

  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[c]);
  const fmt = num => Number(num || 0).toLocaleString("vi-VN");
  const setVisible = (el, show) => { if (el) el.hidden = !show; };
  const formatSeconds = ms => {
    const secs = Math.max(0, Math.ceil(ms / 1000));
    return `${String(Math.floor(secs / 60)).padStart(2, "0")}:${String(secs % 60).padStart(2, "0")}`;
  };
  const notify = (message, good = false) => {
    const el = $("notice");
    if (!el) return;
    el.textContent = message;
    el.classList.toggle("success", good);
    el.hidden = false;
    if (el._hide) clearTimeout(el._hide);
    el._hide = setTimeout(() => { el.hidden = true; }, 7500);
  };
  const isSecret = s => s?.phase === "finale_intro" || s?.phase === "sealed" || s?.is_secret;
  const storageKey = (kind, code) => `city6:${kind}:${code}`;

  async function api(url, method = "GET", data = null) {
    const headers = { "Accept": "application/json" };
    if (data !== null) headers["Content-Type"] = "application/json";
    if (token) headers[role === "teacher" ? "X-City-Teacher" : "X-City-Team"] = token;
    const response = await fetch(url, {
      method, headers, cache: "no-store", credentials: "same-origin",
      body: data === null ? undefined : JSON.stringify(data)
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload.ok) {
      const error = new Error(payload.error || `Máy chủ trả về lỗi ${response.status}.`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function beep(kind) {
    if (!soundOn || role !== "teacher") return;
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return;
      audioContext ||= new AudioCtx();
      if (audioContext.state === "suspended") audioContext.resume().catch(() => {});
      let notes = kind === "win" ? [523, 659, 784, 1046] : kind === "alarm" ? [440, 370] : [740];
      notes.forEach((freq, index) => {
        const osc = audioContext.createOscillator();
        const gain = audioContext.createGain();
        const begin = audioContext.currentTime + index * 0.12;
        osc.type = "sine";
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, begin);
        gain.gain.exponentialRampToValueAtTime(0.08, begin + 0.025);
        gain.gain.exponentialRampToValueAtTime(0.0001, begin + 0.17);
        osc.connect(gain); gain.connect(audioContext.destination);
        osc.start(begin); osc.stop(begin + 0.18);
      });
    } catch (error) { /* Browsers can block audio until user interaction. */ }
  }

  function confetti() {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const colors = ["#8ff9f8", "#fdd27e", "#89f8b0", "#ff91ab", "#a9a0ff"];
    for (let i = 0; i < 35; i++) {
      const e = document.createElement("span");
      e.className = "city-confetti";
      e.style.left = `${Math.random() * 100}vw`;
      e.style.background = colors[i % colors.length];
      e.style.animationDelay = `${Math.random() * .9}s`;
      e.style.animationDuration = `${1.8 + Math.random() * 1.4}s`;
      document.body.appendChild(e);
      setTimeout(() => e.remove(), 4000);
    }
  }

  function updateClock() {
    if (!state) return;
    const now = Date.now() - clockOffset;
    const counting = state.phase === "countdown" || state.phase === "answering";
    const end = state.phase === "countdown" ? state.starts_at_ms : state.ends_at_ms;
    const remaining = counting && end ? Math.max(0, end - now) : 0;
    document.querySelectorAll(".js-clock").forEach(el => {
      el.textContent = counting ? formatSeconds(remaining) : "--:--";
    });
    document.querySelectorAll(".js-countdown-big").forEach(el => {
      el.textContent = String(Math.max(0, Math.ceil(remaining / 1000)));
    });
    document.querySelectorAll(".clock-pill,.team-clock").forEach(el => {
      el.classList.toggle("urgent", state.phase === "answering" && remaining <= 5000);
    });
  }

  async function poll() {
    if (!room || !token || inFlight) return;
    inFlight = true;
    try {
      const s = await api(`${API}/${encodeURIComponent(room)}/trang-thai`);
      clockOffset = Date.now() - s.server_now_ms;
      state = s;
      if (role === "teacher") renderTeacher(s);
      else renderTeam(s);
      updateClock();
    } catch (error) {
      if (error.status === 403 || error.status === 404) {
        token = "";
        state = null;
        localStorage.removeItem(storageKey(role, room));
        if (role === "team") {
          setVisible($("joinPane"), true);
          setVisible($("teamGame"), false);
        } else {
          setVisible($("setup"), true);
          setVisible($("gameArea"), false);
        }
      }
      notify(error.message);
    } finally {
      inFlight = false;
    }
  }

  async function control(action) {
    if (!room || !token) return;
    const button = $("primaryBtn");
    if (button) button.disabled = true;
    try {
      await api(`${API}/${room}/dieu-khien`, "POST", { action });
      await poll();
    } catch (error) { notify(error.message); }
    finally { if (button) button.disabled = false; }
  }

  function displayRanking(rows, small = false) {
    if (!rows || !rows.length) return '<div class="locked-rank">Chưa có bảng xếp hạng.</div>';
    return `<div class="rank-list${small ? ' slim' : ''}">${rows.map(t =>
      `<div class="rank-item ${t.rank <= 3 ? 'top' : ''}">` +
      `<span>${t.rank === 1 ? '🥇' : t.rank === 2 ? '🥈' : t.rank === 3 ? '🥉' : esc(t.rank)}</span>` +
      `<span class="rank-name">${esc(t.icon)} ${esc(t.name)}</span><span class="rank-points">${fmt(t.points)} đ</span></div>`
    ).join("")}</div>`;
  }

  function renderMap(s) {
    $("mapDone").textContent = `${s.stations.filter(v => v.done === 6).length}/5`;
    $("districtList").innerHTML = s.stations.map((d, index) => {
      const active = (s.phase === "answering" || s.phase === "countdown" || s.phase === "review") && Math.floor(s.question_index / 6) === index;
      const klass = active ? "active" : d.state;
      const label = active ? "Đang thực hiện" : d.done === 0 ? "Chưa mở khóa" : d.done < 6 ? "Đang thực hiện" : d.ratio >= 60 ? "Ổn định" : d.ratio >= 40 ? "Đang hồi phục" : "Hoạt động yếu";
      const mark = active ? "⚡" : d.done === 6 ? "✓" : "🔒";
      return `<div class="district ${klass}"><span class="district-icon">${d.icon}</span>` +
        `<span><strong>${esc(d.name)}</strong><small>${d.done}/6 câu · ${esc(label)}</small></span>` +
        `<span class="district-indicator">${mark}</span></div>`;
    }).join("");
  }

  function renderEnergy(s) {
    $("energyLabel").textContent = `${s.energy}/${s.target_energy}`;
    $("energyFill").style.width = `${s.target_energy ? Math.min(100, s.energy / s.target_energy * 100) : 0}%`;
    $("energyPercent").textContent = `${s.target_energy ? Math.round(s.energy / s.target_energy * 100) : 0}% mục tiêu`;
    $("teamCounter").textContent = `${s.team_count} đội`;
    $("energyMessage").textContent = s.reached_goal
      ? "🎉 Đã đạt mục tiêu hợp tác! Thành phố sẵn sàng khôi phục hoàn hảo."
      : "Mỗi đáp án đúng đóng góp 1 năng lượng. Cả lớp cùng hướng tới mục tiêu 60%.";
  }

  function renderRankingPanel(s) {
    const secret = isSecret(s);
    $("rankStatus").textContent = secret ? "ĐÃ KHÓA" : s.leaderboard ? "CÔNG BỐ" : "ĐANG ẨN";
    if (s.leaderboard && !secret) {
      $("rankingContent").innerHTML = displayRanking(s.leaderboard);
    } else {
      $("rankingContent").innerHTML = `<div class="locked-rank">${secret
        ? '🔒 CHUNG KẾT BÍ MẬT<br>Bảng điểm đã được niêm phong. Chỉ mở sau câu 30.'
        : '🔒 Bảng xếp hạng được công bố sau mỗi trạm và sau câu 27.'}</div>`;
    }
  }

  function stageStory(icon, title, paragraph, extra = "") {
    return `<div class="story-screen"><span class="huge-emoji">${icon}</span><h2>${title}</h2><p>${paragraph}</p>${extra}</div>`;
  }

  function teacherQuestion(s) {
    const q = s.question;
    const reviewing = s.phase === "review";
    let html = `<div class="question-level">${esc(q.level)} · ${fmt(q.points)} điểm · Bài ${q.lesson}</div>` +
      `<div class="question-prompt">${esc(q.prompt)}</div>` +
      `<div class="answer-grid">${q.options.map((option, i) => {
        const style = reviewing ? (i === s.review.answer ? " right" : " wrong-option") : "";
        return `<div class="answer-box${style}"><strong>${LABELS[i]}</strong><span>${esc(option)}</span></div>`;
      }).join("")}</div>`;
    if (reviewing) {
      const r = s.review;
      html += `<div class="stage-numbers"><span>✅ Đúng: ${r.correct}</span><span>❌ Sai: ${r.wrong}</span>` +
        `<span>⌛ Chưa trả lời: ${r.timeout}</span></div>`;
      const max = Math.max(1, ...r.choice_counts);
      html += `<div class="review-breakdown">${r.choice_counts.map((count, i) =>
        `<div class="vote-bar ${i === r.answer ? 'correct' : ''}"><strong><span>${LABELS[i]} ${i === r.answer ? '✓' : ''}</span><span>${count} đội</span></strong>` +
        `<div class="bar"><div style="width:${count/max*100}%"></div></div></div>`
      ).join("")}</div>`;
      html += `<div class="review-explain"><strong>💡 Giải thích:</strong> ${esc(r.explanation)}</div>`;
      if (s.question_index === 26) html += `<p class="seal-label">🔒 BẢNG XẾP HẠNG CUỐI CÙNG TRƯỚC VÒNG CHUNG KẾT đang hiển thị bên phải. Bấm tiếp tục để niêm phong.</p>`;
    } else {
      html += `<div class="stage-numbers"><span>📲 Đã gửi: <strong class="js-answered-count">${s.answered_count}</strong>/${s.team_count} đội</span>` +
        `<span>🔐 Đáp án được giữ bí mật đến khi hết giờ</span></div>`;
    }
    return html;
  }

  function awardsContent(s) {
    const rows = s.leaderboard || [];
    const ranked = [2, 1, 3].flatMap(rank => rows.filter(t => t.rank === rank).map(t => ({...t, position: rank})));
    const podium = `<div class="award-podium">${ranked.map(t => `<div class="podium ${t.position === 1 ? 'first' : t.position === 2 ? 'second' : 'third'}">` +
      `<span class="pod-icon">${t.position === 1 ? '🥇' : t.position === 2 ? '🥈' : '🥉'}</span><div>${esc(t.icon)} ${esc(t.name)}</div><div>${fmt(t.points)} điểm</div></div>`).join("")}</div>`;
    return stageStory("🏆", "VINH DANH NHỮNG BIỆT ĐỘI XUẤT SẮC",
      `Năng lượng chung: <b class="story-highlight">${s.energy}/${s.target_energy}</b>. ` +
      (s.reached_goal ? "THÀNH PHỐ ĐÃ KHÔI PHỤC HOÀN HẢO!" : "Cả lớp đã hoàn thành 5 nhiệm vụ và khôi phục thành phố!"),
      podium);
  }

  function stageMarkup(s) {
    const phase = s.phase;
    if (phase === "intro") return stageStory("🌆", "THÀNH PHỐ SỐ ĐANG GẶP SỰ CỐ!",
      "Năm hệ thống trọng yếu đã ngừng hoạt động. Các biệt đội, hãy hợp sức khôi phục thành phố và tranh tài tại 30 thử thách!");
    if (phase === "countdown") return stageStory("🛰️", `ĐANG CHUẨN BỊ CÂU ${s.question_number}`,
      `Nhiệm vụ số ${Math.floor(s.question_index / 6) + 1} đã kết nối. Các đội sẵn sàng!`,
      '<div class="countdown-big js-countdown-big">5</div>');
    if (phase === "answering" || phase === "review") return teacherQuestion(s);
    if (phase === "station") {
      const d = s.stations[Math.floor(s.question_index / 6)];
      return stageStory(d.icon, `ĐÃ KHÔI PHỤC: ${esc(d.name.toUpperCase())}`,
        "Một khu vực của thành phố đã được cấp nguồn. Cảm ơn sự đóng góp của tất cả các biệt đội!",
        `<div class="station-stat"><div class="stat-tile"><b>${d.correct}</b><small>Lượt trả lời đúng</small></div>` +
        `<div class="stat-tile"><b>${d.ratio}%</b><small>Độ chính xác trạm</small></div>` +
        `<div class="stat-tile"><b>${s.energy}</b><small>Năng lượng toàn lớp</small></div></div>`);
    }
    if (phase === "finale_intro") return stageStory("🔒", "CHUNG KẾT BÍ MẬT",
      "Bảng xếp hạng đã được NIÊM PHONG. Ba câu 28–30 quyết định vị trí cuối cùng. Các biệt đội vẫn được tính điểm nhưng không thể xem điểm đối thủ!");
    if (phase === "sealed") return stageStory("🔐", "KẾT QUẢ ĐÃ ĐƯỢC NIÊM PHONG",
      "Đã hoàn thành đủ 30 thử thách. Tổng chỉ huy hãy bấm công bố kết quả để mở bảng xếp hạng cuối cùng!");
    if (phase === "awards" || phase === "finished") return awardsContent(s);
    return stageStory("🏙️", "CHUẨN BỊ NHIỆM VỤ", "Hãy chờ chỉ huy phát lệnh!");
  }

  const advanceButton = s => {
    switch (s.phase) {
      case "lobby": return ["begin", "🚀 BẮT ĐẦU NHIỆM VỤ"];
      case "intro": return ["advance", "⚡ MỞ CÂU 1"];
      case "review":
        if ([5, 11, 17, 23].includes(s.question_index)) return ["advance", "🗺️ KHÔI PHỤC TRẠM · XEM XẾP HẠNG"];
        if (s.question_index === 26) return ["advance", "🔒 NIÊM PHONG THỨ HẠNG"];
        if (s.question_index === 29) return ["advance", "🔐 NIÊM PHONG KẾT QUẢ"];
        return ["advance", `▶ MỞ CÂU ${s.question_number + 1}`];
      case "station": return ["advance", `▶ VÀO TRẠM ${Math.floor(s.question_index / 6) + 2}`];
      case "finale_intro": return ["advance", "⚡ MỞ CÂU 28 · CHUNG KẾT"];
      case "sealed": return ["reveal", "🏆 CÔNG BỐ KẾT QUẢ CHUNG CUỘC"];
      case "awards": return ["finish", "✓ HOÀN TẤT VÀ LƯU KẾT QUẢ"];
      default: return null;
    }
  };

  function renderTeacher(s) {
    setVisible($("setup"), false);
    setVisible($("gameArea"), true);
    const inLobby = s.phase === "lobby";
    setVisible($("lobby"), inLobby);
    setVisible($("playStage"), !inLobby);
    renderMap(s);
    renderEnergy(s);
    renderRankingPanel(s);
    $("phaseLabel").textContent = PHASE_NAMES[s.phase] || s.phase;
    const action = advanceButton(s);
    setVisible($("primaryBtn"), Boolean(action));
    if (action) {
      $("primaryBtn").textContent = action[1];
      $("primaryBtn").dataset.action = action[0];
    }
    setVisible($("retryBtn"), ["countdown", "answering", "review"].includes(s.phase));
    setVisible($("exportBtn"), ["awards", "finished"].includes(s.phase));
    if (inLobby) {
      $("roomCode").textContent = s.code;
      $("lobbyCount").textContent = `${s.team_count}/12`;
      const teams = s.teams || [];
      const cards = teams.map(t => `<div class="team-tile"><span class="tile-icon">${esc(t.icon)}</span>${esc(t.name)}</div>`);
      for (let k = teams.length; k < 12; k++) cards.push('<div class="team-tile placeholder">＋ Đang chờ đội...</div>');
      $("teamGrid").innerHTML = cards.join("");
      $("primaryBtn").disabled = s.team_count < 2;
    } else {
      const station = s.question_index >= 0 ? s.stations[Math.floor(s.question_index / 6)] : null;
      $("stageKicker").textContent = station ? `${station.icon} ${station.name.toUpperCase()}` : "🌆 TỔNG CHỈ HUY";
      $("stageNumber").textContent = s.question_number ? `CÂU ${s.question_number}/30` : "5 TRẠM · 30 CÂU";
      const signature = JSON.stringify([
        s.phase, s.question_index, s.attempt, s.answered_count, s.energy,
        s.leaderboard?.length, s.review?.correct
      ]);
      if (signature !== previousTeacherKey) {
        $("stageContent").innerHTML = stageMarkup(s);
        previousTeacherKey = signature;
      }
      if (s.phase !== lastPhase) {
        if (s.phase === "awards") { beep("win"); confetti(); }
        else if (s.phase === "station") beep("win");
        else if (s.phase === "finale_intro") beep("alarm");
        else if (s.phase === "review") beep("normal");
      }
    }
    lastPhase = s.phase;
  }

  async function createRoom(event) {
    event.preventDefault();
    const form = $("createForm");
    const durations = {};
    for (const level of ["NB", "TH", "VD thấp", "VD cao"]) {
      const input = form.elements.namedItem(level);
      const value = Number(input.value);
      if (!Number.isInteger(value) || value < 10 || value > 180) {
        return notify(`Thời gian mức ${level} phải từ 10 đến 180 giây.`);
      }
      durations[level] = value;
    }
    const btn = $("createBtn"); btn.disabled = true;
    try {
      const data = await api(`${API}/phong`, "POST", { durations });
      room = data.code; token = data.teacher_token;
      localStorage.setItem(storageKey("teacher", room), token);
      history.replaceState(null, "", `/thanh-pho-so?room=${encodeURIComponent(room)}`);
      $("qrImage").src = data.qr_url;
      $("joinLink").href = data.join_url;
      previousTeacherKey = "";
      await poll();
      notify("Đã tạo phòng! Hãy chiếu mã QR cho các đội tham gia.", true);
    } catch (error) { notify(error.message); }
    finally { btn.disabled = false; }
  }

  function setupTeacher() {
    $("createForm").addEventListener("submit", createRoom);
    $("primaryBtn").addEventListener("click", () => {
      const action = $("primaryBtn").dataset.action;
      if (action) control(action);
    });
    $("retryBtn").addEventListener("click", () => {
      if (confirm("Làm lại câu hiện tại cho TẤT CẢ các đội? Mọi đáp án, điểm và thẻ x2 đã dùng ở câu này sẽ được hoàn tác.")) control("replay");
    });
    $("exportBtn").addEventListener("click", downloadReport);
    $("soundBtn").addEventListener("click", () => {
      soundOn = !soundOn;
      localStorage.setItem("city-game-sound", soundOn ? "on" : "off");
      updateSoundButton();
      if (soundOn) beep("normal");
    });
    updateSoundButton();
    if (room && localStorage.getItem(storageKey("teacher", room))) {
      token = localStorage.getItem(storageKey("teacher", room));
      $("qrImage").src = `/thanh-pho-so/qr/${encodeURIComponent(room)}.png`;
      $("joinLink").href = `/thanh-pho-so/tham-gia?room=${encodeURIComponent(room)}`;
      poll();
    }
  }

  function updateSoundButton() {
    $("soundBtn").textContent = soundOn ? "🔊 Âm thanh: Bật" : "🔇 Âm thanh: Tắt";
    $("soundBtn").setAttribute("aria-pressed", String(soundOn));
  }

  async function downloadReport() {
    if (!room || !token) return;
    const btn = $("exportBtn"); btn.disabled = true;
    try {
      const res = await fetch(`${API}/${room}/bao-cao.csv`, {headers: {"X-City-Teacher": token}, cache: "no-store"});
      if (!res.ok) throw new Error("Không xuất được báo cáo. Hãy đảm bảo đã công bố kết quả chung cuộc.");
      const blob = await res.blob();
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = `game6_${room}_ket_qua.csv`;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    } catch (error) { notify(error.message); }
    finally { btn.disabled = false; }
  }

  function chooseIcon(icon) {
    iconChosen = icon;
    document.querySelectorAll(".icon-opt").forEach(button => {
      const active = button.dataset.icon === icon;
      button.classList.toggle("chosen", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }

  async function joinTeam(event) {
    event.preventDefault();
    const code = $("roomInput").value.trim().toUpperCase();
    const name = $("teamName").value.trim();
    if (!/^[A-Z2-9]{6}$/.test(code)) return notify("Mã phòng phải gồm 6 kí tự do giáo viên cung cấp.");
    if (name.length < 2 || name.length > 36) return notify("Tên đội cần từ 2 đến 36 kí tự.");
    const btn = $("joinBtn"); btn.disabled = true;
    try {
      // The join endpoint is deliberately public; no previous team token is sent.
      const res = await fetch(`${API}/${code}/tham-gia`, {
        method: "POST", cache: "no-store", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name, icon: iconChosen})
      });
      const result = await res.json().catch(() => ({}));
      if (!res.ok || !result.ok) throw new Error(result.error || "Không vào được phòng.");
      room = result.code; token = result.team_token;
      localStorage.setItem(storageKey("team", room), token);
      history.replaceState(null, "", `/thanh-pho-so/tham-gia?room=${encodeURIComponent(room)}`);
      previousTeamKey = "";
      await poll();
      notify("Đã tham gia thành công! Hãy chờ giáo viên bắt đầu.", true);
    } catch (error) { notify(error.message); }
    finally { btn.disabled = false; }
  }

  function renderTeam(s) {
    const team = s.my_team;
    if (!team) return;
    setVisible($("joinPane"), false);
    setVisible($("teamGame"), true);
    $("connectStatus").textContent = "● ĐÃ KẾT NỐI";
    $("connectStatus").classList.add("connected");
    $("teamIcon").textContent = team.icon;
    $("teamTitle").textContent = team.name;
    $("myScore").textContent = fmt(team.points);
    $("teamStation").textContent = s.question_index < 0 ? "NHIỆM VỤ CHƯA BẮT ĐẦU" : `TRẠM ${Math.floor(s.question_index / 6) + 1}/5`;
    $("teamQuestion").textContent = `CÂU ${s.question_number}/30`;
    $("teamProgressFill").style.width = `${s.finished_questions / 30 * 100}%`;
    $("teamEnergy").textContent = `⚡ ${s.energy}/${s.target_energy} năng lượng`;
    $("boostStatus").textContent = team.boost_available ? "×2 CÒN 1 THẺ" : "×2 ĐÃ SỬ DỤNG";
    $("teamPhaseLabel").textContent = PHASE_NAMES[s.phase] || s.phase;
    const qKey = `${s.question_index}:${s.attempt}`;
    if (qKey !== lastQuestionKey) {
      selectedChoice = null; selectedBoost = false;
      lastQuestionKey = qKey;
    }
    const signature = JSON.stringify([
      s.phase, s.question_index, s.attempt, team.my_answer, team.my_boost,
      team.points, team.boost_available, s.review?.answer, s.energy, s.leaderboard?.length
    ]);
    if (signature !== previousTeamKey) {
      $("teamStageContent").innerHTML = teamStage(s);
      previousTeamKey = signature;
      bindTeamQuestion(s);
    }
  }

  function teamWaiting(icon, title, message, extra = "") {
    return `<div class="team-empty"><span class="huge-emoji">${icon}</span><h2>${title}</h2><p>${message}</p>${extra}</div>`;
  }

  function teamChoiceGrid(s) {
    const team = s.my_team;
    const locked = team.my_answer !== null;
    return `<div class="choice-list">${s.question.options.map((opt, index) => {
      const cls = locked ? (team.my_answer === index ? "selected locked" : "locked") : (selectedChoice === index ? "selected" : "");
      return `<button class="choice ${cls}" data-choice="${index}" type="button" ${locked ? "disabled" : ""}>` +
        `<span>${LABELS[index]}</span><div>${esc(opt)}</div></button>`;
    }).join("")}</div>`;
  }

  function teamStage(s) {
    const team = s.my_team;
    if (s.phase === "lobby") return teamWaiting("🧑‍🚀", "ĐÃ SẴN SÀNG!",
      `Bạn đã gia nhập phòng ${esc(s.code)}. Hãy chờ Tổng chỉ huy và các đội khác.`);
    if (s.phase === "intro") return teamWaiting("🌆", "NHIỆM VỤ GIẢI CỨU",
      "Năm hệ thống công nghệ gặp sự cố. Hãy cùng đội của mình giải 30 câu để cứu thành phố!");
    if (s.phase === "countdown") return teamWaiting("🚦", `CHUẨN BỊ CÂU ${s.question_number}`,
      "Câu hỏi sắp mở trên điện thoại. Hãy tập trung và sẵn sàng thảo luận!",
      '<div class="countdown-big js-countdown-big">5</div>');
    if (s.phase === "answering") {
      if (team.my_answer !== null) return teamWaiting("📨", "ĐÃ GHI NHẬN ĐÁP ÁN!",
        `Đội đã chọn ${LABELS[team.my_answer]}${team.my_boost ? " cùng thẻ SIÊU NĂNG LƯỢNG ×2" : ""}. Hãy chờ hệ thống chốt câu hỏi.`);
      const q = s.question;
      return `<div class="question-level">${esc(q.level)} · ${q.points} điểm${isSecret(s) ? ' · 🔒 CHUNG KẾT' : ''}</div>` +
        `<div class="team-question">${esc(q.prompt)}</div>` +
        teamChoiceGrid(s) +
        (team.boost_available ? `<div class="boost-card"><div class="boost-heading"><span>⚡ THẺ SIÊU NĂNG LƯỢNG ×2</span><span>CÒN 1 LƯỢT</span></div>` +
          `<div class="boost-sub">Bật thẻ để nhân đôi điểm nếu trả lời đúng. Gửi sai sẽ mất thẻ.</div>` +
          `<button id="boostBtn" class="boost-toggle ${selectedBoost ? 'active' : ''}" type="button">${selectedBoost ? '✓ ĐANG DÙNG THẺ ×2' : 'BẬT THẺ NHÂN ĐÔI'}</button></div>`
          : '<div class="boost-card">⚡ Thẻ nhân đôi đã được sử dụng ở câu trước.</div>') +
        `<button id="sendAnswer" class="btn btn-main btn-block submit-answer" type="button" ${selectedChoice === null ? 'disabled' : ''}>📨 GỬI ĐÁP ÁN</button>`;
    }
    if (s.phase === "review") {
      const answered = team.my_answer !== null;
      const correct = team.my_correct;
      const earned = Number(team.my_earned || 0);
      let html = `<div class="team-result ${!correct ? 'bad' : ''}">` +
        `<div class="result-emoji">${!answered ? '⌛' : correct ? '🎉' : '💡'}</div>` +
        `<strong>${!answered ? 'CHƯA CÓ ĐÁP ÁN' : correct ? 'CHÍNH XÁC!' : 'CẦN ÔN TẬP THÊM'}</strong>` +
        `<p>${!answered ? 'Đội không gửi đáp án kịp thời.' : 'Đội chọn ' + LABELS[team.my_answer]}</p>` +
        `<div class="earned">+${fmt(earned)} điểm</div></div>` +
        `<div class="answer-reveal"><strong>✅ Đáp án đúng: ${LABELS[s.review.answer]}</strong><p>${esc(s.review.answer_text)}</p>` +
        `<p>💡 ${esc(s.review.explanation)}</p></div>`;
      if (s.question_index === 26 && s.leaderboard) {
        html += `<h3>🏆 Xếp hạng cuối trước chung kết</h3>${displayRanking(s.leaderboard, true)}`;
      }
      if (isSecret(s)) html += '<p class="seal-label">🔒 Bảng xếp hạng các đội vẫn được niêm phong đến hết câu 30.</p>';
      return html;
    }
    if (s.phase === "station") {
      const d = s.stations[Math.floor(s.question_index / 6)];
      return teamWaiting(d.icon, "ĐÃ KHÔI PHỤC TRẠM!",
        `Cả lớp hoàn thành ${d.name}. Đội của bạn đang có ${fmt(team.points)} điểm.`,
        `<div class="team-rank">XẾP HẠNG HIỆN TẠI<strong>#${s.leaderboard?.find(x => x.name === team.name)?.rank || '—'}</strong></div>` +
        `<h3>🏆 Bảng xếp hạng các đội</h3>${displayRanking(s.leaderboard, true)}`);
    }
    if (s.phase === "finale_intro") return teamWaiting("🔒", "CHUNG KẾT BÍ MẬT",
      "Ba thử thách cuối quyết định thứ hạng. Bạn vẫn thấy điểm của đội mình, nhưng điểm đối thủ sẽ được giữ bí mật!");
    if (s.phase === "sealed") return teamWaiting("🔐", "KẾT QUẢ ĐÃ NIÊM PHONG",
      "Đã hoàn thành 30 câu hỏi. Hãy chờ giáo viên công bố thứ hạng chung cuộc.");
    if (s.phase === "awards" || s.phase === "finished") {
      const rank = s.leaderboard?.find(x => x.name === team.name);
      return teamWaiting("🏆", "THÀNH PHỐ ĐÃ HỒI SINH!",
        `Đội ${esc(team.name)} đạt ${fmt(team.points)} điểm với ${team.correct} câu trả lời đúng.`,
        `<div class="team-rank">THỨ HẠNG CHUNG CUỘC<strong>#${rank?.rank || '—'}</strong></div>` +
        `<p>⚡ Năng lượng lớp: ${s.energy}/${s.target_energy}. ${s.reached_goal ? 'Đạt mục tiêu hợp tác!' : 'Cả lớp đã cùng hoàn thành nhiệm vụ!'}</p>` +
        `<h3>🏆 Kết quả các đội</h3>${displayRanking(s.leaderboard, true)}`);
    }
    return teamWaiting("🛰️", "ĐANG KẾT NỐI", "Hãy theo dõi màn hình chính.");
  }

  function bindTeamQuestion(s) {
    if (s.phase !== "answering" || s.my_team.my_answer !== null) return;
    document.querySelectorAll(".choice[data-choice]").forEach(button => {
      button.addEventListener("click", () => {
        selectedChoice = Number(button.dataset.choice);
        document.querySelectorAll(".choice[data-choice]").forEach(x => x.classList.toggle("selected", Number(x.dataset.choice) === selectedChoice));
        $("sendAnswer").disabled = false;
      });
    });
    const boost = $("boostBtn");
    if (boost) boost.addEventListener("click", () => {
      selectedBoost = !selectedBoost;
      boost.classList.toggle("active", selectedBoost);
      boost.textContent = selectedBoost ? "✓ ĐANG DÙNG THẺ ×2" : "BẬT THẺ NHÂN ĐÔI";
    });
    $("sendAnswer").addEventListener("click", sendAnswer);
  }

  async function sendAnswer() {
    if (!state || state.phase !== "answering" || selectedChoice === null) return;
    const btn = $("sendAnswer");
    if (btn) btn.disabled = true;
    try {
      await api(`${API}/${room}/tra-loi`, "POST", {
        question_index: state.question_index, attempt: state.attempt,
        choice: selectedChoice, boost: selectedBoost
      });
      await poll();
      notify("Máy chủ đã ghi nhận đáp án của đội!", true);
    } catch (error) {
      notify(error.message);
      await poll();
      if (btn && btn.isConnected && state?.phase === "answering" && state?.my_team?.my_answer === null) btn.disabled = false;
    }
  }

  function setupTeam() {
    const picker = $("iconPicker");
    picker.innerHTML = ICONS.map(icon => `<button type="button" class="icon-opt" data-icon="${icon}" aria-label="Biểu tượng ${icon}" aria-pressed="false">${icon}</button>`).join("");
    picker.querySelectorAll(".icon-opt").forEach(el => el.addEventListener("click", () => chooseIcon(el.dataset.icon)));
    chooseIcon(iconChosen);
    $("joinForm").addEventListener("submit", joinTeam);
    if (room) $("roomInput").value = room;
    if (room && localStorage.getItem(storageKey("team", room))) {
      token = localStorage.getItem(storageKey("team", room));
      poll();
    }
  }

  if (role === "teacher") setupTeacher(); else setupTeam();
  setInterval(poll, 1200);
  setInterval(updateClock, 100);
})();
