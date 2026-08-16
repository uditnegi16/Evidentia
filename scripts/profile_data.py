import sys, pandas as pd
from pathlib import Path

src = Path(sys.argv[1])
df = pd.read_excel(src) if src.suffix in {".xlsx", ".xls"} else pd.read_csv(src)

print("=== DATE ===")
d = pd.to_datetime(df["receivedate"].astype(str), format="%Y%m%d")
print(d.min().date(), "->", d.max().date())
print("report_date matches receivedate:",
      (pd.to_datetime(df["report_date"]).dt.normalize() == d).all())

print("\n=== MULTI-ROW CASES: versions or reactions? ===")
multi = df[df.duplicated("safetyreportid", keep=False)].sort_values("safetyreportid")
g = multi.groupby("safetyreportid")
print("versions differ within case:", (g["safetyreportversion"].nunique() > 1).sum())
print("reaction PT differs within case:",
      (g["patient_reaction_reactionmeddrapt"].nunique() > 1).sum())
print("report_date differs within case:", (g["report_date"].nunique() > 1).sum())
print("\nSample multi-row case:")
first = multi["safetyreportid"].iloc[0]
print(multi[multi["safetyreportid"] == first][
    ["safetyreportid","safetyreportversion","report_date",
     "patient_reaction_reactionmeddrapt"]].to_string())

print("\n=== COMMA SPLITTING ===")
pt = df["patient_reaction_reactionmeddrapt"].fillna("")
n = pt.str.count(",") + 1
print("reactions per row:", n.value_counts().sort_index().to_dict())
print("TOTAL reaction events:", int(n.sum()), "(reference PADER says 3648)")

ex = pt.str.split(",").explode().str.strip()
print("\nTop 8 reactions after split:")
print(ex.value_counts().head(8).to_string())
print("\nreference: AKI 80, Drug ineffective 53, Hypotension 46, "
      "Drug interaction 43, Fatigue 33")

out = df["patient_reaction_reactionoutcome"].fillna("").str.split(",").explode().str.strip()
print("\nOutcome vocabulary after split:", out.value_counts().to_dict())

print("\naligned reaction/outcome lengths:",
      (n == df["patient_reaction_reactionoutcome"].fillna("").str.count(",")+1).mean().round(3))

print("\n=== AGE UNITS ===")
print(df["patient_patientonsetageunit"].value_counts(dropna=False).to_dict())
print(df.groupby("patient_patientonsetageunit")["patient_patientonsetage"]
        .agg(["count","min","max"]).to_string())

print("\n=== DUPLICATE FLAG ===")
print(df["duplicate"].value_counts(dropna=False).to_dict())
print("cases flagged duplicate:",
      df.loc[df["duplicate"].notna(), "safetyreportid"].nunique())

print("\n=== COUNTRY ===")
for c in ["primarysourcecountry","occurcountry","primarysource_reportercountry"]:
    print(f"{c:32s} null={df[c].isna().mean():.3f} top={df[c].value_counts().head(3).to_dict()}")