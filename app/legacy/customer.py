"""The four active customer.ts routes, including source schema mismatches."""

from app.legacy.caisse import rows
from app.legacy.records import create, save


def permission(raw):
    values = raw.get("customer") or {}
    if raw.get("isAdmin"):
        return dict.fromkeys(("canManage", "read", "create", "remove"), True)
    if values.get("canManage") is True:
        return {"canManage": True, "read": True}
    return {
        "canManage": False,
        "read": values.get("read") is True,
        "update": values.get("update"),
        "create": values.get("create") is True,
        "remove": values.get("remove") is True,
    }


async def customers(engine, data, author):
    for info in data["infos"]:
        person = await create(
            engine,
            "buma_person_entity",
            {
                **{
                    k: info.get(k) for k in ("firstname", "lastname", "address", "email", "contact")
                },
                "author": author,
                "type": "PERSONNE",
                "status": True,
            },
        )
        await create(
            engine,
            "buma_customer",
            {
                "person_entity": person["id"],
                "type": info.get("type"),
                "author": author,
                "kind": "CUSTOMER_PDV",
                "status": True,
                "solde": info.get("salary"),
            },
        )
    return {"message": "ok"}


async def sims(engine, data, author):
    for sim in data["sims"]:
        await create(
            engine,
            "buma_sim",
            {
                **{
                    k: (sim or {}).get(k)
                    for k in ("number", "serial", "pincode", "pushcode", "network", "notes")
                },
                "author": author,
                "status": True,
            },
        )
    return {"message": "ok"}


async def pdvs(engine, data, author):
    for pdv in data["pdvs"]:
        identifier = (pdv or {}).get("identifiant")
        matches = (
            await rows(engine, "SELECT * FROM buma_sim WHERE number=:n", {"n": identifier})
            if identifier
            else []
        )
        person = await create(
            engine,
            "buma_person_entity",
            {
                **{k: (pdv or {}).get(k) for k in ("firstname", "lastname", "address", "contact")},
                "author": author,
                "type": "PERSONNE",
                "status": True,
            },
        )
        point = await create(
            engine,
            "buma_pdv",
            {
                "name": pdv.get("name"),
                "soldes": 0,
                "person": person["id"],
                "nina": pdv.get("nina"),
                "rccm": pdv.get("rccm"),
                "rccmaddress": pdv.get("rccm_address"),
                "author": author,
            },
        )
        if not identifier:
            # The source's {id:""} placeholder is truthy but has no set method.
            raise TypeError("simRecord.set is not a function")
        if matches:
            await save(engine, "buma_sim", matches[0]["id"], {"pdv": point["id"]})
        else:
            await create(
                engine,
                "buma_sim",
                {
                    "number": identifier,
                    "serial": "",
                    "pincode": "",
                    "pushcode": "",
                    "network": pdv.get("network"),
                    "notes": "",
                    "pdv": point["id"],
                },
            )
    return {"message": "ok"}


async def import_agent(engine, agent, customer, author):
    # parent is absent from the corrected source schema. Do not substitute agent.
    points = await rows(engine, "SELECT * FROM buma_pdv WHERE parent=:id", {"id": agent})
    for point in points:
        links = await rows(
            engine, "SELECT id FROM buma_customer_pdv WHERE pdv=:id", {"id": point["id"]}
        )
        if not links:
            await create(
                engine,
                "buma_customer_pdv",
                {"customer": customer, "pdv": point["id"], "author": author},
            )
    return {"message": "ok"}
