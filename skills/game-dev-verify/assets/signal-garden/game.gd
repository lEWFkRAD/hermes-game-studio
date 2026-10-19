extends Node2D

const SIZE := Vector2i(9, 5)
const CELL := 76.0
const ORIGIN := Vector2(98, 150)
const EXIT := Vector2i(8, 4)
const HAZARDS := [Vector2i(3, 1), Vector2i(5, 3)]
const CELLS := [Vector2i(2, 0), Vector2i(4, 2), Vector2i(7, 4)]
var player := Vector2i.ZERO
var remaining: Array = []
var state := "playing"
var steps := 0

func _ready() -> void:
    reset_game()

func reset_game() -> void:
    player = Vector2i.ZERO
    remaining = CELLS.duplicate()
    state = "playing"
    steps = 0
    queue_redraw()

func move_player(direction: Vector2i) -> void:
    if state != "playing" or absi(direction.x) + absi(direction.y) != 1:
        return
    var target := player + direction
    if target.x < 0 or target.y < 0 or target.x >= SIZE.x or target.y >= SIZE.y:
        return
    player = target
    steps += 1
    if player in HAZARDS:
        state = "lost"
    else:
        remaining.erase(player)
        if player == EXIT and remaining.is_empty():
            state = "won"
    queue_redraw()

func _unhandled_key_input(event: InputEvent) -> void:
    if not event is InputEventKey or not event.pressed or event.echo:
        return
    match event.keycode:
        KEY_R: reset_game()
        KEY_LEFT: move_player(Vector2i.LEFT)
        KEY_RIGHT: move_player(Vector2i.RIGHT)
        KEY_UP: move_player(Vector2i.UP)
        KEY_DOWN: move_player(Vector2i.DOWN)

func _draw() -> void:
    var font := ThemeDB.fallback_font
    draw_string(font, Vector2(98, 60), "SIGNAL GARDEN", HORIZONTAL_ALIGNMENT_LEFT, -1, 30, Color("edf4ff"))
    draw_string(font, Vector2(98, 98), "Collect 3 cells. Reach the exit. Avoid red tiles.", HORIZONTAL_ALIGNMENT_LEFT, -1, 18, Color("9baec7"))
    for y in range(SIZE.y):
        for x in range(SIZE.x):
            var tile := Vector2i(x, y)
            var box := Rect2(ORIGIN + Vector2(tile) * CELL, Vector2(CELL - 5, CELL - 5))
            var color := Color("17283c")
            if tile in HAZARDS:
                color = Color("8d3446")
            if tile == EXIT:
                color = Color("24775c")
            draw_rect(box, color)
            if tile in remaining:
                draw_circle(box.get_center(), 11, Color("ffd277"))
            if tile == player:
                draw_circle(box.get_center(), 20, Color("76d7ed"))
                draw_circle(box.get_center(), 9, Color("effaff"))
    var message := "Arrow keys: move   R: restart   Cells: %d/3" % (3 - remaining.size())
    if state == "won":
        message = "SIGNAL RESTORED   |   %d steps   |   R: restart" % steps
    elif state == "lost":
        message = "SIGNAL LOST   |   R: retry"
    draw_string(font, Vector2(98, 564), message, HORIZONTAL_ALIGNMENT_LEFT, -1, 18, Color("edf4ff"))
