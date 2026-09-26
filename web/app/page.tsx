export default function HomePage() {
  return (
    <main style={{ padding: "48px 24px", maxWidth: 720, margin: "0 auto" }}>
      <h1 style={{ fontSize: 32, marginBottom: 8 }}>Mooo</h1>
      <p
        style={{
          display: "inline-block",
          padding: "4px 10px",
          borderRadius: 4,
          background: "#1f6f43",
          fontWeight: 700,
          letterSpacing: 1,
        }}
      >
        DEMO TRADING
      </p>
      <p style={{ color: "#9aa4ad" }}>
        Trading terminal placeholder. The default environment is Bybit Demo Trading. Live
        Trading is disabled.
      </p>
    </main>
  );
}
