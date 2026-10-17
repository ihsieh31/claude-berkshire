import sys, json
sys.path.insert(0, "/Users/zongen/Downloads/claude code/berkshire/berkshire-deep-merged/scripts")
import cap_calc

def run(label, **kw):
    x = {"oe": 5.0, "g10": 0.05, "g5": 0.05, "grade": "B", "long_rate": 0.042,
         "long_rate_20y_med": 0.045, "price": 100.0, "norm_eps": 5.0,
         "largecap_pe_median": 26.6}
    x.update(kw)
    r = cap_calc.calc(dict(x))
    print(f"{label:34s} r_d={r['r_d']:.3f} IV_cons={r.get('IV_cons')} IV_base={r.get('IV_base')} "
          f"P2'={r['components']['P2_prime']} cap={r['cap']} auto={r['auto_buy_cap']} g_impl={r.get('g_impl')}")
    return r

print("== 30Y rate sensitivity (everything else equal, oe=5 EPS=5 => E/P=5%) ==")
for L in (0.01, 0.02, 0.03, 0.042, 0.05, 0.07, 0.09):
    run(f"L={L:.1%}", long_rate=L, long_rate_20y_med=0.045)

print("\n== growth sensitivity (L=4.2%) ==")
for g in (0.0, 0.02, 0.05, 0.08, 0.12, 0.20):
    run(f"g={g:.0%}", g10=g, g5=g)

print("\n== grade ==")
for gr in ("A","B","C","D"):
    run(f"grade={gr}", grade=gr)

print("\n== negative history (rule says cap must be IV0 x k) ==")
r = run("g=-2%", g10=-0.02, g5=-0.02)
print("   IV0 =", r.get("IV0"), " rule would cap at k*IV0 =", 0.75*r["IV0"], " actual auto =", r["auto_buy_cap"])

print("\n== missing norm_eps falls back to oe ==")
run("no norm_eps", norm_eps=None)
