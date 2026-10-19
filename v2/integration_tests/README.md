Tests which cross service boundaries on running services. 

Tests here must cross the NATS boundary and are structured as NATS commands in -> results validated out. 

Tests here MAY NOT monkeypatch or otherwise reach into the code. 