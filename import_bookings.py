# -*- coding: utf-8 -*-
"""
Import de reservations dans Lodgify depuis un CSV (relevs Lodgify).
A LANCER SUR LE SERVEUR (il a acces a api.lodgify.com et a la cle).

Usage :
  python3 import_bookings.py --csv import.csv --property "studio hydraulique"          # APERCU (ne cree rien)
  python3 import_bookings.py --csv import.csv --property "studio hydraulique" --go --limit 1   # cree 1 resa (TEST)
  python3 import_bookings.py --csv import.csv --property "studio hydraulique" --go            # cree tout le reste

Regle : on NE recree PAS une reservation si les dates chevauchent une reservation deja presente.
Le CSV peut etre le relev Lodgify (colonnes Arrivee/Checkout/Nom du client/Montant/Devise)
OU un CSV simple (arrival,departure,guest,amount,currency).
"""
import os, sys, csv, re, time, json, argparse, datetime, urllib.request, urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE, ".env"))
except Exception:
    pass

API = "https://api.lodgify.com"
KEY = os.getenv("LODGIFY_API_KEY", "")
MONTHS = {'janv':1,'févr':2,'fevr':2,'mars':3,'avr':4,'mai':5,'juin':6,
          'juil':7,'août':8,'aout':8,'sept':9,'oct':10,'nov':11,'déc':12,'dec':12}


def fix(s):
    if s is None:
        return ''
    try:
        s = s.encode('latin-1').decode('utf-8')   # repare le double-encodage
    except Exception:
        pass
    return s.replace('\xa0', ' ').strip()


def pdate(s):
    s = fix(s)
    # deja ISO ?
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    s = s.replace('.', ' ')
    s = re.sub(r'\s+', ' ', s).strip()
    m = re.match(r'(\d{1,2}) (\S+) (\d{4})', s)
    if not m:
        return None
    mon = m.group(2).lower(); mo = None
    for k, v in MONTHS.items():
        if mon.startswith(k):
            mo = v; break
    return datetime.date(int(m.group(3)), mo, int(m.group(1))) if mo else None


def api_get(path, params=None):
    url = API + path
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    req = urllib.request.Request(url, headers={"accept": "application/json", "X-ApiKey": KEY})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode())


def api_post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=data, method="POST",
                                 headers={"accept": "application/json",
                                          "Content-Type": "application/json",
                                          "X-ApiKey": KEY})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.status, r.read().decode()


def items(x):
    if isinstance(x, dict):
        for k in ("items", "data", "results"):
            if isinstance(x.get(k), list):
                return x[k]
    return x if isinstance(x, list) else []


def first(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def find_property(substr):
    data = api_get("/v2/properties", {"size": 200})
    for p in items(data):
        nm = str(first(p, "internal_name", "name", default="")).lower()
        nm2 = str(first(p, "name", default="")).lower()
        if substr.lower() in nm or substr.lower() in nm2:
            pid = first(p, "id", "property_id")
            return pid, first(p, "internal_name", "name", default=str(pid))
    return None, None


def room_type_id(pid):
    try:
        rooms = items(api_get(f"/v2/properties/{pid}/rooms"))
        if rooms:
            return first(rooms[0], "id", "room_type_id")
    except Exception as e:
        print("  (rooms:", e, ")")
    return None


def existing_ranges(pid):
    """Liste (arrival,departure) des reservations deja presentes pour ce logement."""
    out = []
    for page in range(1, 12):
        try:
            data = api_get("/v2/reservations/bookings", {"size": 50, "page": page})
        except Exception:
            break
        lst = items(data)
        if not lst:
            break
        for b in lst:
            bpid = first(b, "property_id", "propertyId")
            if str(bpid) != str(pid):
                continue
            a = pdate(str(first(b, "arrival", "checkIn", "check_in", default="")) [:10])
            d = pdate(str(first(b, "departure", "checkOut", "check_out", default="")) [:10])
            if a and d:
                out.append((a, d))
        if len(lst) < 50:
            break
    return out


def overlaps(a, d, ranges):
    for (ea, ed) in ranges:
        if a < ed and ea < d:   # chevauchement de periodes
            return (ea, ed)
    return None


def read_csv(path):
    rows = []
    with open(path, encoding='utf-8') as fh:
        for row in csv.DictReader(fh):
            v = {fix(k): val for k, val in row.items()}
            a = pdate(v.get('Arrivée') or v.get('arrival') or '')
            d = pdate(v.get('Checkout') or v.get('departure') or '')
            if not (a and d):
                continue
            rows.append({
                "arrival": a, "departure": d,
                "guest": fix(v.get('Nom du client') or v.get('guest') or ''),
                "amount": fix(v.get('Montant') or v.get('amount') or ''),
                "currency": fix(v.get('Devise') or v.get('currency') or 'EUR'),
            })
    rows.sort(key=lambda r: r["arrival"])
    return rows


def split_name(full):
    full = (full or "").strip()
    if not full:
        return "Client", ""
    parts = full.split()
    return parts[0], " ".join(parts[1:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--property", required=True)
    ap.add_argument("--status", default="Booked")
    ap.add_argument("--adults", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--go", action="store_true")
    a = ap.parse_args()

    if not KEY:
        print("ERREUR: LODGIFY_API_KEY introuvable (.env)."); sys.exit(1)

    pid, pname = find_property(a.property)
    if not pid:
        print(f"ERREUR: logement contenant « {a.property} » introuvable."); sys.exit(1)
    rid = room_type_id(pid)
    print(f"Logement : {pname} (id={pid}, room_type_id={rid})")

    ranges = existing_ranges(pid)
    print(f"Reservations deja presentes pour ce logement : {len(ranges)}")

    rows = read_csv(a.csv)
    print(f"Lignes lues dans le CSV : {len(rows)}\n" + "-"*70)

    todo = []
    for r in rows:
        ov = overlaps(r["arrival"], r["departure"], ranges)
        if ov:
            print(f"SKIP  {r['arrival']}->{r['departure']}  {r['guest'] or '(sans nom)':30} (dej pris {ov[0]}->{ov[1]})")
        else:
            todo.append(r)
            print(f"CREER {r['arrival']}->{r['departure']}  {r['guest'] or '(sans nom)':30} {r['amount']} {r['currency']}")

    if a.limit:
        todo = todo[:a.limit]
    print("-"*70)
    print(f"A creer : {len(todo)}" + ("" if a.go else "   (APERCU — rien cree. Ajoute --go pour creer.)"))
    if not a.go or not todo:
        return

    created, failed = 0, 0
    for r in todo:
        fn, ln = split_name(r["guest"])
        body = {
            "arrival": r["arrival"].isoformat(),
            "departure": r["departure"].isoformat(),
            "property_id": int(pid),
            "status": a.status,
            "source_text": "Import CSV",
            "origin": "Import CSV",
            "guest": {"guest_name": {"first_name": fn, "last_name": ln}},
            "rooms": [{"room_type_id": int(rid), "guest_breakdown": {"adults": a.adults}}],
        }
        if r["amount"]:
            try:
                body["total"] = float(r["amount"]); body["currency_code"] = r["currency"] or "EUR"
            except ValueError:
                pass
        try:
            st, resp = api_post("/v1/reservation/booking", body)
            print(f"OK   {r['arrival']} {r['guest'][:25]:25} -> creee (#{resp.strip()})")
            created += 1
        except urllib.error.HTTPError as e:
            print(f"ERR  {r['arrival']} {r['guest'][:25]:25} -> {e.code} {e.read().decode()[:160]}")
            failed += 1
        except Exception as e:
            print(f"ERR  {r['arrival']} {r['guest'][:25]:25} -> {e}")
            failed += 1
        time.sleep(1.2)   # respecte les limites de l'API
    print("-"*70)
    print(f"Termine. Creees: {created} | Echecs: {failed}")


if __name__ == "__main__":
    main()
