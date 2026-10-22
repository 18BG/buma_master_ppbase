"""Remaining master PDV/rupture behavior from the frozen handlers."""

import json
import math
from datetime import UTC, datetime

from ppbase.models.record import format_datetime

from app.auth.permissions import parse_permission
from app.commerce.sequential import read
from app.legacy.caisse import rows
from app.legacy.records import create, save


async def pdv_permission(engine, role_id):
    role = await read(engine, "roles", role_id or "")
    if role is None:
        raise ValueError("Something went wrong while processing your request.")
    raw = parse_permission(role.get("permission"))
    if raw.get("isAdmin"):
        apps = await rows(engine, "SELECT * FROM buma_apps_roles WHERE name='pdv'", {})
        if not apps:
            raise ValueError("Cannot read property 'length' of undefined")
        info = apps[0].get("info")
        if isinstance(info, str):
            info = json.loads(info)
        for action in (info or {}).get("actions", []):
            raw[action.split(":")[0]] = True
        return raw
    return raw.get("pdv") or {}


async def assign(engine, point_id, agent_id, *, remove=False):
    point = await read(engine, "buma_pdv", point_id)
    if point is None:
        raise ValueError("Point de vente introuvable")
    if remove:
        if not point.get("agent"):
            raise ValueError("Ce point de vente n'est assigné à aucun agent")
        if point["agent"] != agent_id:
            raise ValueError("Ce point de vente est assigné à un autre agent")
    else:
        agent = await read(engine, "buma_agent", agent_id)
        if agent is None:
            raise ValueError("Agent introuvable")
        if point.get("agent") and point["agent"] != agent_id:
            raise ValueError("Ce point de vente est déjà assigné à un autre agent")
    try:
        await save(engine, "buma_pdv", point_id, {"agent": None if remove else agent_id})
    except Exception as exc:
        prefix = (
            "Erreur lors de la dissociation du PDV: "
            if remove
            else "Erreur lors de l'assignation du PDV: "
        )
        raise ValueError(prefix + str(exc)) from exc
    return {
        "success": True,
        "pdv": point_id,
        **({} if remove else {"agent": agent_id}),
        "message": "Point de vente dissocié avec succès"
        if remove
        else "Point de vente assigné avec succès",
    }


def date_part(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed.astimezone(UTC) if parsed.tzinfo else parsed).date()


async def turnovers(engine, data):
    if not data.get("startDate") or not data.get("endDate"):
        raise ValueError("Les dates de début et de fin sont requises")
    try:
        start, end = date_part(data["startDate"]), date_part(data["endDate"])
    except (ValueError, TypeError) as exc:
        raise ValueError("Format de date invalide") from exc
    total_days = (end - start).days + 1
    page, per_page = data.get("page") or 1, data.get("perPage") or 10
    records = await rows(
        engine,
        """SELECT * FROM buma_pdv_turnover
        WHERE to_char(date AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS.MS"Z"') >= :start
        AND to_char(date AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS.MS"Z"') <= :end""",
        {"start": start.isoformat(), "end": end.isoformat()},
    )
    summaries, seen = {}, set()
    for record in records:
        pid, sid = record.get("pdvId"), record.get("sim")
        day = format_datetime(record.get("date"))
        if not pid or not sid or not day or (pid, sid, day) in seen:
            continue
        seen.add((pid, sid, day))
        name = data.get("name")
        if (
            name
            and name.lower() not in (record["firstname"] + " " + record["lastname"]).lower()
            and name.lower() not in record["revendeur"].lower()
        ):
            continue
        if data.get("network") and record["network"] != data["network"]:
            continue
        turnover = {
            "id": record["id"],
            "daily_turnover": int(record.get("daily_turnover") or 0),
            "sim": sid,
            "date": day,
            "daily_goal_turnover": int(record.get("daily_goal_turnover") or 0),
            "pdvId": pid,
        }
        for key in ("msisdn", "revendeur", "network", "firstname", "lastname"):
            turnover[key] = record.get(key) or ""
        for key in ("isNafama", "isPaani", "isSingleWallet"):
            turnover[key] = bool(record.get(key))
        turnover["turnover_single_wallet"] = (
            turnover["daily_turnover"] if turnover["isSingleWallet"] else 0
        )
        turnover["turnover_nafama"] = (
            0 if turnover["isSingleWallet"] else turnover["daily_turnover"]
        )
        summary = summaries.setdefault(
            pid,
            {
                "pdvId": pid,
                "firstname": turnover["firstname"],
                "lastname": turnover["lastname"],
                "number": turnover["revendeur"],
                "totalTurnover": 0,
                "total_turnover_single_wallet": 0,
                "total_turnover_nafama": 0,
                "averageDailyTurnover": 0,
                "averageAchievementRate": 0,
                "numberOfDays": 0,
                "totalDaysInPeriod": total_days,
                "goalTurnover": turnover["daily_goal_turnover"],
                "network": turnover["network"],
                "turnovers": [],
            },
        )
        summary["turnovers"].append(turnover)
        summary["totalTurnover"] += turnover["daily_turnover"]
        summary["total_turnover_single_wallet"] += turnover["turnover_single_wallet"]
        summary["total_turnover_nafama"] += turnover["turnover_nafama"]
        summary["numberOfDays"] += 1
        summary["averageDailyTurnover"] = (
            summary["totalTurnover"] / total_days if total_days else None
        )
        expected = summary["goalTurnover"] * total_days
        summary["averageAchievementRate"] = (
            summary["totalTurnover"] / expected * 100 if expected > 0 else 0
        )
    result = list(summaries.values())
    key = data.get("sortBy") or "date"
    result.sort(
        key=lambda row: (
            row.get(key)
            if isinstance(row.get(key), (int, float))
            else str(row.get(key, "undefined")).lower()
        ),
        reverse=(data.get("sortOrder") or "desc") != "asc",
    )
    count = len(result)
    offset = int((float(page) - 1) * float(per_page))
    return {
        "data": result[offset : offset + int(per_page)],
        "pagination": {
            "page": page,
            "perPage": per_page,
            "totalRecords": count,
            "totalPages": math.ceil(count / float(per_page)),
        },
    }


async def rupture(engine, data):
    if not isinstance(data.get("data"), list):
        return 400, {"message": "Données manquantes ou format invalide"}
    author, file_name = data.get("user_id") or None, data.get("file_name") or None
    total = data.get("total_records") or len(data["data"])
    results = []
    for item in data["data"]:
        if item is None:
            raise ValueError("Cannot read property 'Number' of undefined")
        if not item.get("Number") or type(item.get("Balance")) not in (int, float):
            continue
        found = await rows(
            engine, "SELECT * FROM buma_rupture WHERE number=:n", {"n": item["Number"]}
        )
        values = {"balance": item["Balance"], "user_id": author, "file_name": file_name}
        if found:
            await save(engine, "buma_rupture", found[0]["id"], values)
        else:
            await create(engine, "buma_rupture", {"number": item["Number"], **values})
        results.append(
            {
                "action": "updated" if found else "created",
                "number": item["Number"],
                "balance": item["Balance"],
            }
        )
    return {
        "message": "Données de rupture sauvegardées avec succès",
        "total_processed": len(results),
        "total_received": total,
        "user_id": author,
        "file_name": file_name,
        "results": results,
    }
