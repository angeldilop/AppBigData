import os
import tempfile

import streamlit as st
from dotenv import load_dotenv
from pymongo import MongoClient
from neo4j import GraphDatabase
import neo4j  # para TrustAll (Neo4j Aura)
from pyvis.network import Network
import streamlit.components.v1 as components


# =========================
#   CONEXIONES A BD
# =========================

@st.cache_resource
def get_mongo():
    load_dotenv()
    uri = os.getenv("MONGODB_URI")
    db_name = os.getenv("DB_NAME", "proyecto_bigdata")
    col_name = os.getenv("COLLECTION_NAME", "providencias")

    if not uri:
        raise RuntimeError("MONGODB_URI no está definido en .env")

    client = MongoClient(uri)
    db = client[db_name]
    col_prov = db[col_name]
    col_sim = db["similitudes"]
    return client, db, col_prov, col_sim


@st.cache_resource
def get_neo4j_driver():
    load_dotenv()
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USERNAME")
    password = os.getenv("NEO4J_PASSWORD")
    database = os.getenv("NEO4J_DATABASE", "neo4j")

    if not uri or not user or not password:
        raise RuntimeError("Faltan variables de entorno de Neo4j")

    driver = GraphDatabase.driver(
        uri,
        auth=(user, password),
        encrypted=True,
        trusted_certificates=neo4j.TrustAll(),
    )
    return driver, database


# =========================
#   FUNCIONES DE NEGOCIO
# =========================

def mostrar_detalle_providencia(prov_id: str):
    """Muestra el detalle de una providencia (consulta Mongo)."""
    try:
        _, _, col_prov, _ = get_mongo()
    except Exception as e:
        st.error(f"Error de conexión a MongoDB: {e}")
        return

    doc = col_prov.find_one({"providencia": prov_id}, {"_id": 0})

    if not doc:
        st.warning(f"No se encontró la providencia {prov_id}.")
        return

    providencia = doc.get("providencia", "")
    anio = doc.get("anio", "")
    tipo = doc.get("tipo", "")
    texto = doc.get("texto", "(Sin texto disponible)")

    st.subheader(f"Providencia {providencia}")
    st.markdown(f"**Año:** {anio} &nbsp;&nbsp;|&nbsp;&nbsp; **Tipo:** {tipo}")

    st.markdown("**Texto completo**")
    st.text_area(
        label="",
        value=texto,
        height=350,
    )


def buscar_por_texto(q: str):
    """Búsqueda por texto usando índice de texto de Mongo."""
    try:
        _, _, col_prov, _ = get_mongo()
    except Exception as e:
        st.error(f"Error de conexión a MongoDB: {e}")
        return

    # Se asume que el índice de texto ya fue creado en la ETL
    cursor = col_prov.find(
        {"$text": {"$search": q}},
        {"_id": 0, "texto": 1, "providencia": 1, "anio": 1, "tipo": 1},
    )

    docs = list(cursor)

    if not docs:
        st.info("No se encontraron providencias que coincidan con esa búsqueda de texto.")
        return

    st.write(f"Se encontraron **{len(docs)}** providencias que contienen el texto buscado.")
    for d in docs:
        prov = d.get("providencia", "")
        anio = d.get("anio", "")
        tipo = d.get("tipo", "")
        texto = d.get("texto", "")
        snippet = (texto[:300] + "...") if len(texto) > 300 else texto

        with st.expander(f"{prov} · {tipo} · {anio}"):
            st.write(snippet)
            if st.button(f"Ver detalle de {prov}", key=f"detalle_{prov}"):
                mostrar_detalle_providencia(prov)


def ver_similitudes(prov_id: str, min_sim: float):
    """
    Consulta la colección 'similitudes' en Mongo.
    """
    try:
        _, _, _, col_sim = get_mongo()
    except Exception as e:
        st.error(f"Error de conexión a MongoDB: {e}")
        return

    sims = list(col_sim.find(
        {
            "$or": [
                {"providencia1": prov_id},
                {"providencia2": prov_id}
            ]
        },
        {"_id": 0}
    ))

    if not sims:
        st.info("No hay registros de similitud para esa providencia en la colección `similitudes`.")
        return

    # Construimos un diccionario {otro: max(similitud)}
    scores_por_otro = {}

    for s in sims:
        p1 = s.get("providencia1")
        p2 = s.get("providencia2")
        score = s.get("similitud")

        if score is None:
            continue

        score = float(score)

        if p1 == prov_id:
            otro = p2
        else:
            otro = p1

        if not otro:
            continue

        # Nos quedamos con la similitud más alta para ese "otro"
        if otro in scores_por_otro:
            scores_por_otro[otro] = max(scores_por_otro[otro], score)
        else:
            scores_por_otro[otro] = score

    if not scores_por_otro:
        st.info("No se pudieron calcular similitudes válidas para esta providencia.")
        return

    # Rango real de similitudes
    all_scores = list(scores_por_otro.values())
    st.caption(
        f"Las similitudes disponibles para {prov_id} están entre "
        f"{min(all_scores):.2f} y {max(all_scores):.2f}. "
        f"(Umbral actual en el control: {min_sim:.2f})"
    )

    # Aplicamos el umbral al diccionario ya fusionado
    sims_filtradas = [
        {"otro": otro, "similitud": sim}
        for otro, sim in scores_por_otro.items()
        if sim >= min_sim
    ]

    sims_filtradas.sort(key=lambda x: x["similitud"], reverse=True)

    if not sims_filtradas:
        st.info(
            f"No se encontraron providencias similares con umbral ≥ {min_sim:.2f}. "
            "Prueba con un valor más bajo dentro del rango mostrado."
        )
        return

    st.write(f"Se encontraron **{len(sims_filtradas)}** providencias similares (≥ {min_sim:.2f}):")

    for idx, s in enumerate(sims_filtradas):
        otro = s["otro"]
        sim = s["similitud"]
        cols = st.columns([3, 1])
        with cols[0]:
            st.markdown(f"- **{otro}** (similitud: **{sim:.4f}**)")
        with cols[1]:
            # idx se usa para evitar claves duplicadas
            if st.button(f"Ver {otro}", key=f"sim_{prov_id}_{otro}_{idx}"):
                mostrar_detalle_providencia(otro)


def show_graph_for_providencia(prov_id: str, min_sim: float):
    """
    Consulta Neo4j y dibuja el grafo con Pyvis.
    El label del nodo se enriquece con datos de MongoDB y los ajustes de visualización.
    """
    try:
        driver, database = get_neo4j_driver()
        _, _, col_prov, _ = get_mongo()  # Necesitamos Mongo para obtener TIPO y AÑO
    except Exception as e:
        st.error(f"Error de conexión: No se pudo conectar a Neo4j o a MongoDB. Causa: {e}")
        return

    # 1. Función para obtener metadatos de MongoDB (para etiquetas enriquecidas)
    @st.cache_data
    def get_prov_metadata(p_id):
        doc = col_prov.find_one({"providencia": p_id}, {"_id": 0, "tipo": 1, "anio": 1})
        if doc:
            return doc.get("tipo", "Tipo?"), doc.get("anio", "Año?")
        return "Tipo?", "Año?"

    # Obtener detalles del nodo raíz desde Mongo para la etiqueta
    root_tipo, root_anio = get_prov_metadata(prov_id)
    # Etiqueta enriquecida: ID y metadatos en dos líneas (usando \n)
    root_label = f"{prov_id}\n({root_tipo}-{root_anio})"
    root_title = f"ID: {prov_id}, Tipo: {root_tipo}, Año: {root_anio}"


    with driver.session(database=database) as session:
        query = """
        MATCH (p:Providencia {id: $id})-[r:SIMILAR_A]->(q:Providencia)
        WHERE r.similitud >= $min_sim
        RETURN p.id AS origen, q.id AS destino, r.similitud AS similitud, q.tipo AS tipo_q, q.anio AS anio_q
        ORDER BY similitud DESC
        """
        # Se añaden las propiedades del nodo vecino (q.tipo, q.anio) al RETURN de Cypher
        rows = session.run(query, id=prov_id, min_sim=min_sim).data()

    if not rows:
        st.info("No se encontraron vecinos en el grafo con esa similitud mínima.")
        return

    # Grafo base
    net = Network(height="750px", width="100%", directed=True)
    net.barnes_hut()

    # Opciones de física para un mejor layout y asegurar formato JSON correcto
    net.set_options("""
    {
      "physics": {
        "barnesHut": {
          "springLength": 130
        }
      },
      "edges": {
        "smooth": {
          "type": "continuous"
        }
      },
      "nodes": {
        "shape": "circle",
        "font": {
          "multi": "html",
          "align": "center"
        }
      }
    }
    """)

    # Nodo raíz (providencia seleccionada): AZUL
    net.add_node(
        prov_id,
        label=root_label,
        title=root_title,
        color="#7887F5",
        font={"color": "#0C0909", "size": 14, "face": "arial", "align": "center"}, # Ajuste de tamaño de fuente
        size=30,  # Aumento del tamaño del nodo para acomodar más texto
        shape="circle" 
    )

    added_nodes = {prov_id}

    for row in rows:
        origen = row["origen"]
        destino = row["destino"]
        sim = float(row["similitud"])

        # Datos del nodo vecino
        tipo_q = row.get("tipo_q", "Tipo?")
        anio_q = row.get("anio_q", "Año?")
        neighbor_label = f"{destino}\n({tipo_q}-{anio_q})"
        neighbor_title = f"ID: {destino}, Tipo: {tipo_q}, Año: {anio_q}"


        # Aseguramos que los nodos vecinos se añadan con la etiqueta enriquecida
        if destino not in added_nodes:
            net.add_node(
                destino,
                label=neighbor_label,
                title=neighbor_title,
                color="#55F57D",  # Verde para vecinos
                font={"color": "#0C0909", "size": 10, "face": "arial", "align": "center"}, # Ajuste de tamaño de fuente
                size=20, # Aumento del tamaño del nodo vecino
                shape="circle" 
            )
            added_nodes.add(destino)

        # Aristas con etiqueta de similitud
        net.add_edge(
            origen,
            destino,
            title=f"Similitud: {sim:.4f}",
            label=f"{sim:.2f}",
            width=3, 
            color="#7887F5",
            arrows="to",
            length=250
        )

    # Renderizamos el HTML y lo incrustamos en Streamlit
    with tempfile.NamedTemporaryFile(delete=False, suffix=".html") as tmp:
        net.write_html(tmp.name, notebook=False)
        html = open(tmp.name, "r", encoding="utf-8").read()

    components.html(html, height=750, scrolling=True)


def explorador_providencias():
    """Explorador maestro-detalle con filtros por código, tipo y año."""
    st.subheader("Explorador de providencias")

    try:
        client, db, col_prov, _ = get_mongo()
    except Exception as e:
        st.error(f"Error de conexión a MongoDB: {e}")
        return

    docs = list(col_prov.find({}, {"_id": 0}))

    if not docs:
        st.info("No hay providencias cargadas en la base de datos.")
        return

    # Tipos y años disponibles
    tipos = sorted({d.get("tipo", "").strip() for d in docs if d.get("tipo")})
    anios_raw = []
    for d in docs:
        anio = d.get("anio")
        try:
            anios_raw.append(int(anio))
        except Exception:
            continue

    if anios_raw:
        min_year = min(anios_raw)
        max_year = max(anios_raw)
    else:
        min_year = 2000
        max_year = 2030

    st.markdown("Filtra las providencias por código, tipo y rango de años:")

    col_f1, col_f2, col_f3 = st.columns([2, 2, 3])

    with col_f1:
        codigo = st.text_input("Código de providencia", placeholder="Ej: A053-24")

    with col_f2:
        tipo_sel = st.selectbox(
            "Tipo de providencia",
            ["Todos"] + tipos,
        )

    with col_f3:
        rango_anios = st.slider(
            "Rango de años",
            min_value=min_year,
            max_value=max_year,
            value=(min_year, max_year),
            step=1,
        )

    if "selected_prov" not in st.session_state:
        st.session_state["selected_prov"] = None

    # Aplicar filtros
    if st.button("Buscar en explorador"):
        filtrados = []

        for d in docs:
            prov = d.get("providencia", "")
            tipo = d.get("tipo", "")
            anio = d.get("anio")

            # Filtro por código
            if codigo.strip() and prov.strip() != codigo.strip():
                continue

            # Filtro por tipo
            if tipo_sel != "Todos" and tipo != tipo_sel:
                continue

            # Filtro por año
            ok_year = True
            try:
                anio_int = int(anio)
                if not (rango_anios[0] <= anio_int <= rango_anios[1]):
                    ok_year = False
            except Exception:
                ok_year = False

            if not ok_year:
                continue

            filtrados.append(d)

        st.session_state["expl_resultados"] = filtrados

        # Selección inicial
        if codigo.strip():
            st.session_state["selected_prov"] = codigo.strip()
        elif filtrados:
            st.session_state["selected_prov"] = filtrados[0].get("providencia", None)

    resultados = st.session_state.get("expl_resultados", docs)

    st.markdown("---")
    st.write(f"Se encontraron **{len(resultados)}** providencias con los filtros actuales.")

    col_lista, col_detalle = st.columns([2, 3])

    with col_lista:
        st.markdown("### Lista de providencias")
        for d in resultados:
            prov = d.get("providencia", "")
            tipo = d.get("tipo", "")
            anio = d.get("anio", "")

            with st.container():
                st.markdown(f"**{prov}** · {tipo} · {anio}")
                if st.button("Ver detalle", key=f"expl_{prov}"):
                    st.session_state["selected_prov"] = prov
                st.markdown("---")

    with col_detalle:
        st.markdown("### Detalle de la providencia seleccionada")
        sel = st.session_state.get("selected_prov")
        if sel:
            mostrar_detalle_providencia(sel)
        else:
            st.info("Selecciona una providencia de la lista para ver el detalle.")


# =========================
#   INTERFAZ STREAMLIT
# =========================

def main():
    st.set_page_config(
        page_title="JurisAudio Insight – App de consulta",
        layout="wide"
    )

    # Estado inicial del menú
    if "menu" not in st.session_state:
        st.session_state["menu"] = "Inicio"

    # ==== ESTILOS GLOBALES (COLORES CORPORATIVOS) ====
    st.markdown(
        """
        <style>
        /* Sidebar gris claro */
        [data-testid="stSidebar"] {
            background-color: #A09984;
            color: #0C0909;
        }

        /* Títulos y texto principal */
        h1, h2, h3, h4, h5, h6 {
            color: #0C0909;
        }
        body, p {
            color: #0C0909;
        }

        /* Botones en contenido principal: AZUL + blanco, sin cambios en hover */
        div.stButton > button {
            background-color: #7887F5;
            color: #FFFFFF;
            border-radius: 8px;
            border: none;
            padding: 0.4rem 0.8rem;
        }
        div.stButton > button:hover {
            background-color: #7887F5;
            color: #FFFFFF;
            border: none;
        }

        /* Botones del sidebar estilo texto, sin cambio de color en hover */
        [data-testid="stSidebar"] .stButton > button {
            background: transparent;
            color: #0C0909;
            border: none;
            text-align: left;
            padding: 0.3rem 0.2rem;
            font-size: 0.95rem;
        }
        [data-testid="stSidebar"] .stButton > button:hover {
            background: transparent;
            color: #0C0909;
            border: none;
        }

        /* Slider con color de la paleta */
        .stSlider [data-baseweb="slider"] > div > div {
            background-color: #7887F5 !important;
        }
        .stSlider [data-baseweb="slider"] [role="slider"] {
            background-color: #7887F5 !important;
            border: none !important;
        }
        .stSlider [data-testid="stTickBarMin"],
        .stSlider [data-testid="stTickBarMax"],
        .stSlider [data-testid="stSliderValue"] {
            background: transparent !important;
            color: #0C0909 !important;
            border: none !important;
        }

        /* Quitar resaltado azul en inputs activos */
        input, textarea, select {
            box-shadow: none !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # ==== MENÚ LATERAL (solo textos clicables) ====
    with st.sidebar:
        if st.button("Inicio"):
            st.session_state["menu"] = "Inicio"

        if st.button("Explorador de providencias"):
            st.session_state["menu"] = "Explorador de providencias"

        if st.button("Buscar por texto"):
            st.session_state["menu"] = "Buscar por texto"

        if st.button("Ver similitudes"):
            st.session_state["menu"] = "Ver similitudes"

        if st.button("Grafo interactivo"):
            st.session_state["menu"] = "Grafo interactivo"

    menu = st.session_state["menu"]

    # ==== CONTENIDO PRINCIPAL ====
    st.title("JurisAudio Insight – Explorador interactivo de providencias")

    st.markdown(
        """
        Esta aplicación permite consultar las providencias judiciales transcritas desde audio,
        almacenadas en MongoDB, y explorar sus relaciones de similitud en un grafo Neo4j.

        Usa el menú lateral para elegir el tipo de consulta.
        """
    )

    if menu == "Inicio":
        st.subheader("Resumen funcional")

        st.markdown(
            """
            Bienvenida a **JurisAudio Insight**, un panel que conecta el procesamiento de audios
            judiciales con herramientas de análisis y exploración de providencias.
            """
        )

        c1, c2, c3, c4 = st.columns(4)

        with c1:
            st.markdown("### Explorador")
            st.markdown(
                "Consulta providencias por código, tipo y año, con un panel maestro-detalle para leer el texto completo."
            )

        with c2:
            st.markdown("### Búsqueda por texto")
            st.markdown(
                "Encuentra providencias que contengan palabras o expresiones clave dentro del contenido transcrito."
            )

        with c3:
            st.markdown("### Similitudes")
            st.markdown(
                "Identifica las providencias más cercanas a un caso base según similitud TF-IDF almacenada en MongoDB."
            )

        with c4:
            st.markdown("### Grafo Neo4j")
            st.markdown(
                "Explora visualmente la red de providencias similares mediante un grafo interactivo sobre Neo4j."
            )

        st.markdown("---")
        st.markdown(
            """
            #### ¿Por dónde empezar?

            1. Ve al **Explorador de providencias** y selecciona un código (por ejemplo, `A053-24`).  
            2. Revisa su contenido y, si lo deseas, consulta sus **similitudes**.  
            3. Abre el **Grafo interactivo** para ver cómo se conecta con otros casos.
            """
        )

    elif menu == "Explorador de providencias":
        explorador_providencias()

    elif menu == "Buscar por texto":
        st.subheader("Buscar providencias por texto")
        st.markdown(
            "Escribe una palabra o frase, y se buscará en el contenido transcrito de las providencias."
        )
        q = st.text_input("Texto a buscar", value="jurisdicción")
        if st.button("Buscar en texto"):
            if q.strip():
                buscar_por_texto(q.strip())
            else:
                st.warning("Por favor ingresa un texto para buscar.")

    elif menu == "Ver similitudes":
        st.subheader("Ver similitudes de una providencia (MongoDB)")
        st.markdown(
            "Consulta las providencias más parecidas a una providencia base, según la similitud TF-IDF calculada y guardada en la colección `similitudes`."
        )
        prov_id = st.text_input("Código de providencia", value="A053-24", key="sim_prov")
        min_sim = st.slider(
            "Similitud mínima",
            min_value=0.0,
            max_value=1.0,
            value=0.6,
            step=0.05,
        )
        if st.button("Consultar similitudes"):
            if prov_id.strip():
                ver_similitudes(prov_id.strip(), min_sim)
            else:
                st.warning("Por favor ingresa un código de providencia.")

    elif menu == "Grafo interactivo":
        st.subheader("Grafo interactivo de similitud (Neo4j)")
        st.markdown(
            "A partir de una providencia raíz, se muestran las relaciones `SIMILAR_A` con otras providencias, "
            "filtradas por un umbral mínimo de similitud."
        )
        prov_id = st.text_input("Código de providencia", value="A053-24", key="grafo_prov")
        min_sim = st.slider(
            "Similitud mínima para el grafo",
            min_value=0.0,
            max_value=1.0,
            value=0.6,
            step=0.05,
            key="grafo_slider"
        )
        if st.button("Ver grafo"):
            if prov_id.strip():
                show_graph_for_providencia(prov_id.strip(), min_sim)
            else:
                st.warning("Por favor ingresa un código de providencia.")


if __name__ == "__main__":
    main()