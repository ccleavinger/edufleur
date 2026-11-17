use axum::{
    Json, Router,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    routing::{get, post},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
};
use tower_http::services::{ServeDir, ServeFile};

#[derive(Clone)]
struct App {
    db: Arc<libsql::Database>,
    catalog: Arc<Catalog>,
}

#[derive(Default)]
struct Catalog {
    version: Option<String>,
    courses: Vec<Course>,
    coverage: Value,
    images: HashSet<String>,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct Course {
    id: String,
    state: String,
    state_code: String,
    ecoregion_code: String,
    habitat: String,
    description: String,
    available: bool,
    reason: Option<String>,
    total: usize,
    catalog_version: String,
    units: Vec<Unit>,
}

#[derive(Deserialize, Serialize)]
struct Unit {
    id: String,
    title: String,
    plants: Vec<Value>,
    questions: Vec<Question>,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct Question {
    id: String,
    #[serde(skip_serializing)]
    taxon_id: String,
    #[serde(skip_serializing)]
    exercise_type: String,
    prompt: String,
    image: Value,
    options: Vec<String>,
    #[serde(skip_serializing)]
    correct: usize,
    #[serde(skip_serializing)]
    explanation: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Answer {
    question: String,
    choice: usize,
    catalog_version: String,
}

#[derive(Default, Deserialize)]
struct Filters {
    state: Option<String>,
    ecoregion: Option<String>,
}

#[derive(Deserialize)]
struct Report {
    reason: String,
    #[serde(default)]
    details: String,
}

type Error = (StatusCode, &'static str);

fn internal(e: impl std::fmt::Display) -> Error {
    tracing::error!("{e}");
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        "Storage unavailable. Try again.",
    )
}
fn learner(headers: &HeaderMap) -> Result<String, Error> {
    let id = headers
        .get("x-learner-id")
        .and_then(|h| h.to_str().ok())
        .unwrap_or("");
    if id.len() != 36
        || id.chars().enumerate().any(|(i, c)| {
            if [8, 13, 18, 23].contains(&i) {
                c != '-'
            } else {
                !c.is_ascii_hexdigit()
            }
        })
    {
        return Err((StatusCode::BAD_REQUEST, "Invalid learner ID"));
    }
    Ok(id.to_ascii_lowercase())
}
fn course<'a>(app: &'a App, id: &str) -> Result<&'a Course, Error> {
    app.catalog
        .courses
        .iter()
        .find(|c| c.id == id)
        .ok_or((StatusCode::NOT_FOUND, "Course not found"))
}
async fn catalog(State(app): State<App>) -> Json<Value> {
    Json(
        json!({"ready":app.catalog.version.is_some(),"version":app.catalog.version,"coverage":app.catalog.coverage}),
    )
}
async fn courses(State(app): State<App>, Query(filters): Query<Filters>) -> Json<Value> {
    Json(json!(app.catalog.courses.iter()
        .filter(|c| filters.state.as_ref().is_none_or(|s| s.eq_ignore_ascii_case(&c.state_code)))
        .filter(|c| filters.ecoregion.as_ref().is_none_or(|e| e == &c.ecoregion_code))
        .map(|c| json!({"id":c.id,"state":c.state,"stateCode":c.state_code,"ecoregionCode":c.ecoregion_code,"habitat":c.habitat,"description":c.description,"available":c.available,"reason":c.reason,"total":c.total,"unitCount":c.units.len(),"catalogVersion":c.catalog_version}))
        .collect::<Vec<_>>()))
}
async fn lesson(State(app): State<App>, Path(id): Path<String>) -> Result<Json<Value>, Error> {
    Ok(Json(json!(course(&app, &id)?)))
}
async fn progress(State(app): State<App>, headers: HeaderMap) -> Result<Json<Value>, Error> {
    let id = learner(&headers)?;
    let active: HashSet<_> = app
        .catalog
        .courses
        .iter()
        .flat_map(|c| {
            c.units.iter().flat_map(move |u| {
                u.questions
                    .iter()
                    .map(move |q| (c.id.as_str(), q.taxon_id.as_str(), q.exercise_type.as_str()))
            })
        })
        .collect();
    let conn = app.db.connect().map_err(internal)?;
    let mut rows = conn
        .query(
            "SELECT course, taxon, exercise FROM mastery_v2 WHERE learner = ?",
            [id],
        )
        .await
        .map_err(internal)?;
    let mut result = HashMap::<String, usize>::new();
    while let Some(row) = rows.next().await.map_err(internal)? {
        let course = row.get::<String>(0).map_err(internal)?;
        let taxon = row.get::<String>(1).map_err(internal)?;
        let exercise = row.get::<String>(2).map_err(internal)?;
        if active.contains(&(course.as_str(), taxon.as_str(), exercise.as_str())) {
            *result.entry(course).or_default() += 1;
        }
    }
    Ok(Json(json!(result)))
}
async fn answer(
    State(app): State<App>,
    Path(id): Path<String>,
    headers: HeaderMap,
    Json(input): Json<Answer>,
) -> Result<Json<Value>, Error> {
    let user = learner(&headers)?;
    let c = course(&app, &id)?;
    if input.catalog_version != c.catalog_version {
        return Err((
            StatusCode::CONFLICT,
            "The catalog changed. Reload this course before answering.",
        ));
    }
    if !c.available {
        return Err((
            StatusCode::CONFLICT,
            "This course does not yet meet publication requirements.",
        ));
    }
    let q = c
        .units
        .iter()
        .flat_map(|u| &u.questions)
        .find(|q| q.id == input.question)
        .ok_or((StatusCode::BAD_REQUEST, "Unknown question"))?;
    if input.choice >= q.options.len() {
        return Err((StatusCode::BAD_REQUEST, "Unknown choice"));
    }
    let correct = input.choice == q.correct;
    if correct {
        app.db.connect().map_err(internal)?.execute("INSERT OR IGNORE INTO mastery_v2 (learner, course, taxon, exercise) VALUES (?, ?, ?, ?)",libsql::params![user,id,q.taxon_id.clone(),q.exercise_type.clone()]).await.map_err(internal)?;
    }
    Ok(Json(
        json!({"correct":correct,"explanation":q.explanation,"correctName":q.options[q.correct]}),
    ))
}
async fn report(
    State(app): State<App>,
    Path(image): Path<String>,
    headers: HeaderMap,
    Json(input): Json<Report>,
) -> Result<(StatusCode, Json<Value>), Error> {
    let user = learner(&headers)?;
    if !app.catalog.images.contains(&image) {
        return Err((StatusCode::NOT_FOUND, "Image not found"));
    }
    if !["wrongPlant", "unhelpful", "license", "other"].contains(&input.reason.as_str())
        || input.details.chars().count() > 500
    {
        return Err((
            StatusCode::BAD_REQUEST,
            "Choose a report reason and use at most 500 characters.",
        ));
    }
    app.db.connect().map_err(internal)?.execute("INSERT INTO image_reports (learner,image,reason,details,catalog_version) VALUES (?,?,?,?,?) ON CONFLICT(learner,image) DO UPDATE SET reason=excluded.reason,details=excluded.details,catalog_version=excluded.catalog_version",libsql::params![user,image,input.reason,input.details,app.catalog.version.clone().unwrap_or_default()]).await.map_err(internal)?;
    Ok((StatusCode::CREATED, Json(json!({"received":true}))))
}
async fn load_catalog(directory: &std::path::Path) -> Result<Catalog, Box<dyn std::error::Error>> {
    let path = directory.join("catalog.db");
    if !path.exists() {
        tracing::warn!(
            "No published catalog at {}. Habitat browser will show an empty state.",
            path.display()
        );
        return Ok(Catalog::default());
    }
    let db = libsql::Builder::new_local(path)
        .flags(libsql::OpenFlags::SQLITE_OPEN_READ_ONLY)
        .build()
        .await?;
    let conn = db.connect()?;
    let mut rows = conn.query("SELECT key,value FROM metadata", ()).await?;
    let mut metadata = HashMap::<String, Value>::new();
    while let Some(row) = rows.next().await? {
        metadata.insert(row.get(0)?, serde_json::from_str(&row.get::<String>(1)?)?);
    }
    if metadata.get("schemaVersion") != Some(&json!(1)) {
        return Err("Unsupported catalog schema".into());
    }
    let version = metadata
        .get("catalogVersion")
        .and_then(Value::as_str)
        .ok_or("Missing catalog version")?
        .to_owned();
    let mut rows = conn
        .query("SELECT payload FROM courses ORDER BY id", ())
        .await?;
    let mut courses = Vec::new();
    while let Some(row) = rows.next().await? {
        let c: Course = serde_json::from_str(&row.get::<String>(0)?)?;
        if c.catalog_version != version
            || c.units
                .iter()
                .flat_map(|u| &u.questions)
                .any(|q| q.options.len() != 4 || q.correct >= q.options.len())
        {
            return Err("Invalid generated course".into());
        }
        courses.push(c);
    }
    let mut images = HashSet::new();
    let mut rows = conn.query("SELECT id FROM images", ()).await?;
    while let Some(row) = rows.next().await? {
        images.insert(row.get(0)?);
    }
    Ok(Catalog {
        version: Some(version),
        courses,
        coverage: metadata.remove("coverage").unwrap_or(Value::Null),
        images,
    })
}
fn router(app: App, directory: &std::path::Path) -> Router {
    Router::new()
        .route("/api/health", get(|| async { "ok" }))
        .route("/api/catalog", get(catalog))
        .route("/api/courses", get(courses))
        .route("/api/courses/{id}", get(lesson))
        .route("/api/courses/{id}/answers", post(answer))
        .route("/api/progress", get(progress))
        .route("/api/images/{image}/reports", post(report))
        .route("/api/{*path}", get(|| async { StatusCode::NOT_FOUND }))
        .nest_service("/media", ServeDir::new(directory.join("images")))
        .fallback_service(
            ServeDir::new("frontend/dist").fallback(ServeFile::new("frontend/dist/index.html")),
        )
        .with_state(app)
}

const PROGRESS_SCHEMA: &str = "PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS mastery_v2 (learner TEXT NOT NULL,course TEXT NOT NULL,taxon TEXT NOT NULL,exercise TEXT NOT NULL,PRIMARY KEY(learner,course,taxon,exercise));
        CREATE TABLE IF NOT EXISTS image_reports (id INTEGER PRIMARY KEY,learner TEXT NOT NULL,image TEXT NOT NULL,reason TEXT NOT NULL,details TEXT NOT NULL,catalog_version TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(learner,image));";

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();
    let db =
        libsql::Builder::new_local(std::env::var("DATABASE_PATH").unwrap_or("edufleur.db".into()))
            .build()
            .await?;
    // v1 mastery remains intact; generated content uses taxon identity, not question order.
    db.connect()?.execute_batch(PROGRESS_SCHEMA).await?;
    let directory = std::path::PathBuf::from(
        std::env::var("CATALOG_DIR").unwrap_or("data/catalog/current".into()),
    );
    // Resolve once: image serving and the in-memory catalog use the same immutable bundle.
    let directory = if directory.exists() {
        directory.canonicalize()?
    } else {
        directory
    };
    let app = App {
        db: Arc::new(db),
        catalog: Arc::new(load_catalog(&directory).await?),
    };
    let router = router(app, &directory);
    let address = std::env::var("BIND_ADDRESS").unwrap_or("127.0.0.1:3000".into());
    tracing::info!("Listening on {address}");
    axum::serve(tokio::net::TcpListener::bind(address).await?, router).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        body::{Body, to_bytes},
        http::Request,
    };
    use tower::ServiceExt;

    struct Fixture(std::path::PathBuf);
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    async fn request(
        router: &Router,
        method: &str,
        path: &str,
        body: Option<Value>,
    ) -> (StatusCode, Vec<u8>) {
        let req = Request::builder()
            .method(method)
            .uri(path)
            .header("x-learner-id", "12345678-1234-1234-1234-123456789abc")
            .header("content-type", "application/json")
            .body(Body::from(body.map(|v| v.to_string()).unwrap_or_default()))
            .unwrap();
        let response = router.clone().oneshot(req).await.unwrap();
        let status = response.status();
        let bytes = to_bytes(response.into_body(), 2_000_000)
            .await
            .unwrap()
            .to_vec();
        (status, bytes)
    }
    async fn json_request(
        router: &Router,
        method: &str,
        path: &str,
        body: Option<Value>,
    ) -> (StatusCode, Value) {
        let (status, bytes) = request(router, method, path, body).await;
        (
            status,
            serde_json::from_slice(&bytes)
                .unwrap_or_else(|_| json!(String::from_utf8_lossy(&bytes))),
        )
    }
    #[tokio::test]
    async fn generated_catalog_http_contract_and_rebuild() {
        let fixture = Fixture(std::env::temp_dir().join(format!(
                "edufleur-http-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            )));
        let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .to_owned();
        let output = std::process::Command::new("python3")
            .arg(root.join("tests/prepare_api_fixture.py"))
            .arg(&fixture.0)
            .current_dir(&root)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let db = Arc::new(
            libsql::Builder::new_local(fixture.0.join("progress.db"))
                .build()
                .await
                .unwrap(),
        );
        db.connect()
            .unwrap()
            .execute_batch(PROGRESS_SCHEMA)
            .await
            .unwrap();
        let catalog = Arc::new(load_catalog(&fixture.0.join("bundle")).await.unwrap());
        let c = catalog.courses.iter().find(|c| c.available).unwrap();
        let q = &c.units[0].questions[0];
        let answer = json!({"question":q.id,"choice":q.correct,"catalogVersion":c.catalog_version});
        let path = format!("/api/courses/{}/answers", c.id);
        let route = router(
            App {
                db: db.clone(),
                catalog: catalog.clone(),
            },
            &fixture.0.join("bundle"),
        );
        assert_eq!(
            json_request(&route, "GET", "/api/catalog", None).await.1["ready"],
            true
        );
        let filtered = json_request(&route, "GET", "/api/courses?state=CO&ecoregion=TEST", None)
            .await
            .1;
        assert_eq!(filtered.as_array().unwrap().len(), 1);
        assert_eq!(
            json_request(&route, "GET", "/api/courses?state=WA", None)
                .await
                .1,
            json!([])
        );
        let detail = json_request(&route, "GET", "/api/courses/co-epa3-TEST", None)
            .await
            .1;
        let public = &detail["units"][0]["questions"][0];
        for field in ["correct", "taxonId", "explanation", "exerciseType"] {
            assert!(public.get(field).is_none());
        }
        assert_eq!(
            request(&route, "GET", q.image["url"].as_str().unwrap(), None)
                .await
                .0,
            StatusCode::OK
        );
        let mut wrong = answer.clone();
        wrong["choice"] = json!((q.correct + 1) % 4);
        assert_eq!(
            json_request(&route, "POST", &path, Some(wrong)).await.1["correct"],
            false
        );
        assert_eq!(
            json_request(&route, "GET", "/api/progress", None).await.1,
            json!({})
        );
        for _ in 0..2 {
            assert_eq!(
                json_request(&route, "POST", &path, Some(answer.clone()))
                    .await
                    .1["correct"],
                true
            );
        }
        assert_eq!(
            json_request(&route, "GET", "/api/progress", None).await.1,
            json!({"co-epa3-TEST":1})
        );
        for (field, value, status) in [
            ("catalogVersion", json!("old"), StatusCode::CONFLICT),
            ("choice", json!(99), StatusCode::BAD_REQUEST),
            ("question", json!("missing"), StatusCode::BAD_REQUEST),
        ] {
            let mut invalid = answer.clone();
            invalid[field] = value;
            assert_eq!(
                request(&route, "POST", &path, Some(invalid)).await.0,
                status
            );
        }
        let image = q.image["id"].as_str().unwrap();
        let report_path = format!("/api/images/{image}/reports");
        for _ in 0..2 {
            assert_eq!(
                request(
                    &route,
                    "POST",
                    &report_path,
                    Some(json!({"reason":"wrongPlant","details":"Inspect this"}))
                )
                .await
                .0,
                StatusCode::CREATED
            );
        }
        for body in [
            json!({"reason":"invented"}),
            json!({"reason":"other","details":"x".repeat(501)}),
        ] {
            assert_eq!(
                request(&route, "POST", &report_path, Some(body)).await.0,
                StatusCode::BAD_REQUEST
            );
        }
        let conn = db.connect().unwrap();
        let mut rows = conn
            .query(
                "SELECT (SELECT COUNT(*) FROM mastery), (SELECT COUNT(*) FROM image_reports)",
                (),
            )
            .await
            .unwrap();
        let row = rows.next().await.unwrap().unwrap();
        assert_eq!(row.get::<i64>(0).unwrap(), 1);
        assert_eq!(row.get::<i64>(1).unwrap(), 1);
        let next = Arc::new(load_catalog(&fixture.0.join("next-bundle")).await.unwrap());
        assert_ne!(catalog.version, next.version);
        let rebuilt = router(
            App {
                db: db.clone(),
                catalog: next,
            },
            &fixture.0.join("next-bundle"),
        );
        assert_eq!(
            json_request(&rebuilt, "GET", "/api/progress", None).await.1,
            json!({"co-epa3-TEST":1})
        );
        assert_eq!(
            request(&rebuilt, "POST", &path, Some(answer)).await.0,
            StatusCode::CONFLICT
        );
        let empty = router(
            App {
                db,
                catalog: Arc::new(load_catalog(&fixture.0.join("missing")).await.unwrap()),
            },
            &fixture.0.join("missing"),
        );
        assert_eq!(
            json_request(&empty, "GET", "/api/catalog", None).await.1["ready"],
            false
        );
        assert_eq!(
            json_request(&empty, "GET", "/api/courses", None).await.1,
            json!([])
        );
        assert_eq!(
            request(&empty, "GET", "/api/courses/missing", None).await.0,
            StatusCode::NOT_FOUND
        );
        // Deep links are served by Axum's SPA fallback once the frontend is built.
        if root.join("frontend/dist/index.html").exists() {
            // Tests may run from backend/ rather than the repository root.
            let service = ServeDir::new(root.join("frontend/dist"))
                .fallback(ServeFile::new(root.join("frontend/dist/index.html")));
            let response = service
                .oneshot(
                    Request::builder()
                        .uri("/habitats?state=CO")
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::OK);
        }
    }
    #[test]
    fn learner_identifier_is_validated() {
        let mut headers = HeaderMap::new();
        assert!(learner(&headers).is_err());
        headers.insert(
            "x-learner-id",
            "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx".parse().unwrap(),
        );
        assert!(learner(&headers).is_err());
        headers.insert(
            "x-learner-id",
            "ABCDEF12-1234-1234-1234-123456789ABC".parse().unwrap(),
        );
        assert_eq!(
            learner(&headers).unwrap(),
            "abcdef12-1234-1234-1234-123456789abc"
        );
    }
}
