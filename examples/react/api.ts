export async function login(baseUrl: string, email: string, password: string) {
  const res = await fetch(`${baseUrl}/api/v1/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password })
  });
  if (!res.ok) throw new Error("Login failed");
  return res.json();
}

export async function getMe(baseUrl: string, accessToken: string) {
  const res = await fetch(`${baseUrl}/api/v1/me`, {
    headers: { Authorization: `Bearer ${accessToken}` }
  });
  if (!res.ok) throw new Error("Fetch me failed");
  return res.json();
}
