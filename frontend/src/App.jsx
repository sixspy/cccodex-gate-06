import { useEffect, useState } from "react";

function loadProfile() {
  try {
    return JSON.parse(localStorage.getItem("lake.profile") || "null");
  } catch {
    return null;
  }
}

async function request(path, options = {}) {
  const token = localStorage.getItem("lake.token") || "";
  const headers = { ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "请求没有完成");
  }
  return data;
}

const POLICIES = [
  { value: "queue_block", name: "队列拦收", hint: "挡在坏号队列不收成" },
  { value: "pad_to_detail", name: "缺位补位", hint: "补齐缺位后再入库到明细" },
];

const TABS = [
  { key: "queue", label: "坏号队列" },
  { key: "boxes", label: "箱号列表" },
  { key: "completion", label: "补码详情" },
  { key: "layers", label: "贴源 / 明细" },
];

export default function App() {
  const [profile, setProfile] = useState(loadProfile);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("123456");
  const [loginError, setLoginError] = useState("");
  const [dates, setDates] = useState([]);
  const [detail, setDetail] = useState(null);
  const [board, setBoard] = useState(null);
  const [tab, setTab] = useState("queue");
  const [formError, setFormError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [fixedInputs, setFixedInputs] = useState({});

  const isWriter = profile?.role === "writer";

  async function refresh(prefer) {
    const rows = await request("/api/dates");
    setDates(rows);
    const target = prefer || detail?.biz_date || rows[0]?.biz_date;
    if (target) {
      const [dayData, boardData] = await Promise.all([
        request(`/api/dates/${target}`),
        request(`/api/dates/${target}/boxes`),
      ]);
      setDetail(dayData);
      setBoard(boardData);
    }
  }

  useEffect(() => {
    if (!profile) return;
    refresh().catch(() => {
      localStorage.removeItem("lake.token");
      localStorage.removeItem("lake.profile");
      setProfile(null);
    });
  }, [profile]);

  async function switchDate(bizDate) {
    setFormError("");
    setNotice("");
    const [dayData, boardData] = await Promise.all([
      request(`/api/dates/${bizDate}`),
      request(`/api/dates/${bizDate}/boxes`),
    ]);
    setDetail(dayData);
    setBoard(boardData);
  }

  async function submitLogin(event) {
    event.preventDefault();
    setLoginError("");
    try {
      const data = await request("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      localStorage.setItem("lake.token", data.access_token);
      const next = { username: data.username, role: data.role, title: data.title };
      localStorage.setItem("lake.profile", JSON.stringify(next));
      setProfile(next);
    } catch (error) {
      setLoginError(error.message);
    }
  }

  async function savePolicy(policy) {
    if (!detail) return;
    setBusy(true);
    setFormError("");
    setNotice("");
    try {
      await request(`/api/dates/${detail.biz_date}/box-policy`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ policy }),
      });
      await refresh(detail.biz_date);
      setNotice("补码办法已写定，本日不许改口");
    } catch (error) {
      setFormError(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function resolveBox(rawBox, policy, fixedBox = "") {
    if (!detail) return;
    setBusy(true);
    setFormError("");
    setNotice("");
    try {
      await request(`/api/dates/${detail.biz_date}/completions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ policy, raw_box: rawBox, fixed_box: fixedBox }),
      });
      await refresh(detail.biz_date);
      setNotice(fixedBox ? "已补齐缺位，重新收成后入明细" : "已拦在坏号队列，不收成");
    } catch (error) {
      setFormError(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function buildDay() {
    if (!detail || !board?.policy) return;
    setBusy(true);
    setFormError("");
    setNotice("");
    try {
      const data = await request(`/api/dates/${detail.biz_date}/build`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ policy: board.policy.policy }),
      });
      setDetail(data);
      setDates(await request("/api/dates"));
      setBoard(await request(`/api/dates/${detail.biz_date}/boxes`));
      setNotice("已按写定的补码办法收成明细");
    } catch (error) {
      setFormError(error.message);
    } finally {
      setBusy(false);
    }
  }

  function logout() {
    localStorage.removeItem("lake.token");
    localStorage.removeItem("lake.profile");
    setProfile(null);
    setDetail(null);
    setBoard(null);
  }

  if (!profile) {
    return (
      <main className="sheet gate">
        <p className="kicker">贴源层 → 明细层</p>
        <h1>闸口事件湖仓台</h1>
        <p>扫描原文先落贴源。坏箱号按本日写定的补码办法处理，全空扫描不进坏号队列。</p>
        <form onSubmit={submitLogin}>
          <label>用户<input value={username} onChange={(event) => setUsername(event.target.value)} /></label>
          <label>口令<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          <button>打开这一层</button>
          {loginError ? <p className="warn">{loginError}</p> : null}
        </form>
      </main>
    );
  }

  const policy = board?.policy || null;
  const compliant = (board?.details || []).filter((row) => row.box_source === "compliant");
  const padded = (board?.details || []).filter((row) => row.box_source === "padded");
  const pendingQueue = (board?.queue || []).filter((row) => row.status === "pending");
  const doneQueue = (board?.queue || []).filter((row) => row.status !== "pending");

  return (
    <>
      <header className="bar">
        <div>
          <p className="kicker">闸口扫描 · 分层 · 补码</p>
          <h1>闸口事件湖仓台</h1>
        </div>
        <div className="who">
          <span>{profile.title} {profile.username}</span>
          <button className="line" onClick={logout}>离开</button>
        </div>
      </header>
      <main className="layout">
        <aside>
          {dates.map((row) => (
            <button
              key={row.biz_date}
              className={detail?.biz_date === row.biz_date ? "day on" : "day"}
              onClick={() => switchDate(row.biz_date)}
            >
              <strong>{row.biz_date}</strong>
              <em>{row.built ? "明细已收" : "尚未构建"}</em>
              <span>贴源 {row.ods_count} 条 · 明细 {row.dwd_count} 条</span>
              <span>{row.policy_name ? `办法：${row.policy_name}` : "办法未写定"}</span>
            </button>
          ))}
        </aside>
        {detail && board ? (
          <section className="sheet">
            <div className="split-head">
              <h2>{detail.biz_date}</h2>
              <button
                disabled={!isWriter || busy || !policy}
                title={!policy ? "补码尚未写定，不准收成" : ""}
                onClick={buildDay}
              >
                按补码办法收成明细
              </button>
            </div>
            {!policy ? <p className="warn">补码尚未写定，不准收成这一天的坏号。</p> : null}
            {formError ? <p className="warn">{formError}</p> : null}
            {notice ? <p className="ok">{notice}</p> : null}

            <article className="policy-card">
              <h3>补码设置</h3>
              {policy ? (
                <div className="policy-locked">
                  <span className="badge badge-on">{policy.policy_name}</span>
                  <span>
                    {policy.set_by} 于 {policy.set_at} 写定，本日不许改口
                  </span>
                </div>
              ) : (
                <div className="policy-options">
                  {POLICIES.map((item) => (
                    <button
                      key={item.value}
                      className="policy-btn"
                      disabled={!isWriter || busy}
                      onClick={() => savePolicy(item.value)}
                    >
                      <strong>{item.name}</strong>
                      <em>{item.hint}</em>
                    </button>
                  ))}
                  {!isWriter ? <p className="note">核对员不能写定办法。</p> : null}
                </div>
              )}
            </article>

            <nav className="tabs">
              {TABS.map((item) => (
                <button
                  key={item.key}
                  className={tab === item.key ? "tab on" : "tab"}
                  onClick={() => setTab(item.key)}
                >
                  {item.label}
                </button>
              ))}
            </nav>

            {tab === "queue" ? (
              <article>
                <h3>坏号队列</h3>
                <p className="note">只收非空且不合规的箱号；箱号全空的扫描不进队列。两侧抢同一条时，只放行与办法相符的第一记。</p>
                {pendingQueue.length ? (
                  <table>
                    <thead>
                      <tr><th>坏号</th><th>剔除次数</th><th>处理</th></tr>
                    </thead>
                    <tbody>
                      {pendingQueue.map((row) => (
                        <tr key={row.id}>
                          <td><code>{row.raw_box}</code></td>
                          <td>{row.reject_count}</td>
                          <td>
                            {!isWriter || !policy ? <span className="note">待作业员处理</span> :
                              policy.policy === "queue_block" ? (
                                <button
                                  className="mini"
                                  disabled={busy}
                                  onClick={() => resolveBox(row.raw_box, "queue_block")}
                                >
                                  拦收（不入明细）
                                </button>
                              ) : (
                                <span className="pad-row">
                                  <input
                                    placeholder="补齐后的完整箱号"
                                    value={fixedInputs[row.raw_box] || ""}
                                    onChange={(event) =>
                                      setFixedInputs((prev) => ({ ...prev, [row.raw_box]: event.target.value }))
                                    }
                                  />
                                  <button
                                    className="mini"
                                    disabled={busy}
                                    onClick={() =>
                                      resolveBox(row.raw_box, "pad_to_detail", fixedInputs[row.raw_box] || "")
                                    }
                                  >
                                    补位入库
                                  </button>
                                </span>
                              )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : <p className="note">没有待处理的坏号。</p>}

                <h3>已处理</h3>
                {doneQueue.length ? (
                  <table>
                    <thead>
                      <tr><th>坏号</th><th>状态</th><th>补成箱号</th><th>处理人</th><th>时间</th></tr>
                    </thead>
                    <tbody>
                      {doneQueue.map((row) => (
                        <tr key={row.id}>
                          <td><code>{row.raw_box}</code></td>
                          <td><span className={`badge badge-${row.status}`}>{row.status_name}</span></td>
                          <td>{row.fixed_box ? <code>{row.fixed_box}</code> : "—"}</td>
                          <td>{row.fixed_by}</td>
                          <td>{row.fixed_at}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : <p className="note">还没有处理记录。</p>}

                <h3>补码台账（办法不符 / 争用留痕）</h3>
                {board.attempts.length ? (
                  <table>
                    <thead>
                      <tr><th>时间</th><th>坏号</th><th>请求办法</th><th>结果</th><th>说明</th><th>作业员</th></tr>
                    </thead>
                    <tbody>
                      {board.attempts.map((row) => (
                        <tr key={row.id}>
                          <td>{row.attempted_at}</td>
                          <td><code>{row.raw_box}</code></td>
                          <td>{row.policy_name}</td>
                          <td><span className={`badge badge-${row.result}`}>{row.result_name}</span></td>
                          <td>{row.detail}</td>
                          <td>{row.operator}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : <p className="note">还没有补码尝试。</p>}
              </article>
            ) : null}

            {tab === "boxes" ? (
              <article>
                <h3>箱号列表</h3>
                <table>
                  <thead>
                    <tr><th>坏号</th><th>剔除次数</th><th>补成次数</th><th>办法名</th><th>状态</th></tr>
                  </thead>
                  <tbody>
                    {board.queue.length ? board.queue.map((row) => (
                      <tr key={row.id}>
                        <td><code>{row.raw_box}</code></td>
                        <td>{row.reject_count}</td>
                        <td>{row.fixed_count}</td>
                        <td>{row.policy_name || "办法未写定"}</td>
                        <td><span className={`badge badge-${row.status}`}>{row.status_name}</span></td>
                      </tr>
                    )) : (
                      <tr><td colSpan="5" className="note">这一天没有坏号，全空扫描不进队列。</td></tr>
                    )}
                  </tbody>
                </table>
              </article>
            ) : null}

            {tab === "completion" ? (
              <article>
                <h3>补码详情 · 已合规</h3>
                {compliant.length ? (
                  <table>
                    <thead>
                      <tr><th>原文箱号</th><th>入库箱号</th><th>闸口</th><th>事件</th><th>时间</th></tr>
                    </thead>
                    <tbody>
                      {compliant.map((row) => (
                        <tr key={row.source_id}>
                          <td><code>{row.raw_box}</code></td>
                          <td><code>{row.container_no}</code></td>
                          <td>{row.terminal_code}</td>
                          <td>{row.event_kind}</td>
                          <td>{row.event_at}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : <p className="note">没有已合规明细。</p>}

                <h3>补码详情 · 已补位</h3>
                {padded.length ? (
                  <table>
                    <thead>
                      <tr><th>原文箱号</th><th>补成后的箱号</th><th>闸口</th><th>事件</th><th>时间</th></tr>
                    </thead>
                    <tbody>
                      {padded.map((row) => (
                        <tr key={row.source_id} className="row-padded">
                          <td><code className="raw-box">{row.raw_box}</code></td>
                          <td><code className="fixed-box">{row.container_no}</code></td>
                          <td>{row.terminal_code}</td>
                          <td>{row.event_kind}</td>
                          <td>{row.event_at}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : <p className="note">还没有补位入库的明细。</p>}
              </article>
            ) : null}

            {tab === "layers" ? (
              <div className="layers">
                <article>
                  <h3>贴源层</h3>
                  <ul>
                    {detail.ods.map((row) => (
                      <li key={row.id}>
                        <span>{row.terminal_code}</span>
                        <code>{JSON.stringify(row.raw_payload)}</code>
                      </li>
                    ))}
                  </ul>
                </article>
                <article>
                  <h3>明细层</h3>
                  {detail.moves.length ? (
                    <table>
                      <thead>
                        <tr><th>来源</th><th>箱号</th><th>来源标记</th><th>事件</th><th>时间</th></tr>
                      </thead>
                      <tbody>
                        {detail.moves.map((row) => (
                          <tr key={`${row.source_id}-${row.event_kind}`}>
                            <td>{row.source_id}</td>
                            <td><code>{row.container_no}</code></td>
                            <td>
                              <span className={`badge badge-${row.box_source}`}>
                                {row.box_source === "padded" ? "已补位" : "已合规"}
                              </span>
                            </td>
                            <td>{row.event_kind}</td>
                            <td>{row.event_at}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ) : <p>这一天的明细还是空的。</p>}
                  <h3>剔除</h3>
                  <ul>
                    {detail.rejects.map((row) => (
                      <li key={row.source_id}><span>来源 {row.source_id}</span><em>{row.reason}</em></li>
                    ))}
                    {!detail.rejects.length ? <li>没有被剔除的扫描</li> : null}
                  </ul>
                  {detail.run ? (
                    <p className="note">
                      上次收成：收 {detail.run.accepted_count} 条、剔 {detail.run.rejected_count} 条
                      {detail.run.policy_used_name ? `，请求办法：${detail.run.policy_used_name}` : ""}
                    </p>
                  ) : null}
                </article>
              </div>
            ) : null}
          </section>
        ) : null}
      </main>
    </>
  );
}
