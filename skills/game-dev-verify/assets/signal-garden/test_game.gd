extends SceneTree

var failures := 0
var checks := 0

func expect(condition: bool, label: String) -> void:
    checks += 1
    if not condition:
        failures += 1
        printerr("CHECK_FAILED: " + label)

func _initialize() -> void:
    var game = load("res://game.gd").new()
    root.add_child(game)
    game.reset_game()
    expect(game.player == Vector2i.ZERO and game.remaining.size() == 3 and game.state == "playing", "fresh state")
    game.move_player(Vector2i.LEFT)
    expect(game.player == Vector2i.ZERO and game.steps == 0, "bounds reject without consuming turn")
    game.move_player(Vector2i(1, 1))
    expect(game.steps == 0, "diagonal rejected")
    game.move_player(Vector2i.RIGHT)
    game.move_player(Vector2i.RIGHT)
    expect(game.remaining.size() == 2, "cell collected")
    game.move_player(Vector2i.LEFT)
    game.move_player(Vector2i.RIGHT)
    expect(game.remaining.size() == 2, "cell not collected twice")
    game.move_player(Vector2i.RIGHT)
    game.move_player(Vector2i.DOWN)
    expect(game.state == "lost", "hazard loses")
    var stopped = game.player
    game.move_player(Vector2i.RIGHT)
    expect(game.player == stopped, "loss locks movement")
    game.reset_game()
    expect(game.remaining.size() == 3 and game.steps == 0 and game.state == "playing", "reset restores state")
    for i in range(8):
        game.move_player(Vector2i.RIGHT)
    for i in range(4):
        game.move_player(Vector2i.DOWN)
    expect(game.player == game.EXIT and game.state == "playing", "exit requires every cell")
    game.reset_game()
    for i in range(4):
        game.move_player(Vector2i.RIGHT)
    for i in range(2):
        game.move_player(Vector2i.DOWN)
    for i in range(3):
        game.move_player(Vector2i.RIGHT)
    for i in range(2):
        game.move_player(Vector2i.DOWN)
    game.move_player(Vector2i.RIGHT)
    expect(game.state == "won" and game.remaining.is_empty(), "reachable winning route")
    game.move_player(Vector2i.LEFT)
    expect(game.player == game.EXIT, "win locks movement")
    game.reset_game()
    expect(game.player == Vector2i.ZERO and game.state == "playing" and game.remaining.size() == 3, "restart after win")
    game.free()
    if failures == 0:
        print("STUDIO_GAMEPLAY_PASS checks=" + str(checks))
    quit(1 if failures else 0)
