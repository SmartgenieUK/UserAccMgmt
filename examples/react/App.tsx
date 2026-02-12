import React, { useState } from "react";
import { login, getMe } from "./api";

const BASE_URL = "http://localhost:8000";

export default function App() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [me, setMe] = useState<any>(null);

  const handleLogin = async () => {
    const data = await login(BASE_URL, email, password);
    setAccessToken(data.access_token);
  };

  const handleGetMe = async () => {
    const data = await getMe(BASE_URL, accessToken);
    setMe(data);
  };

  return (
    <div style={{ padding: 24 }}>
      <h1>Auth Reference App</h1>
      <input placeholder="email" value={email} onChange={(e) => setEmail(e.target.value)} />
      <input placeholder="password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
      <button onClick={handleLogin}>Login</button>
      <button onClick={handleGetMe} disabled={!accessToken}>Get Me</button>
      <pre>{me ? JSON.stringify(me, null, 2) : "No profile loaded"}</pre>
    </div>
  );
}
