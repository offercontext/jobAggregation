from offerpilot.db import init_database
from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository
from offerpilot.repositories.jd import JDAnalysesRepository, JDAnalysisCreate


def test_bind_keeps_jd_reads_on_caller_owned_session(tmp_path):
    session_factory = init_database(tmp_path / "data.db")
    application = ApplicationsRepository(session_factory).create(
        ApplicationCreate(company_name="A", position_name="Backend")
    )
    repository = JDAnalysesRepository(session_factory)
    analysis = repository.create(
        JDAnalysisCreate(
            application_id=application.id,
            jd_source="manual",
            jd_text="text",
            result="{}",
        )
    )

    with session_factory() as session:
        bound = repository.bind(session)
        assert bound.get(analysis.id) is not None
        assert [item.id for item in bound.list(application_id=application.id)] == [analysis.id]
