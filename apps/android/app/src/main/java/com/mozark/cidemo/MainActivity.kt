package com.mozark.cidemo

import android.app.Activity
import android.os.Bundle
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.TextView

// Four screens in one Activity: home -> search -> results -> play.
class MainActivity : Activity() {
    private lateinit var title: TextView
    private lateinit var openSearch: Button
    private lateinit var input: EditText
    private lateinit var submit: Button
    private lateinit var result: TextView
    private lateinit var play: Button
    private lateinit var player: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        title = findViewById(R.id.screen_title)
        openSearch = findViewById(R.id.open_search)
        input = findViewById(R.id.search_input)
        submit = findViewById(R.id.search_submit)
        result = findViewById(R.id.result_item)
        play = findViewById(R.id.play_button)
        player = findViewById(R.id.player_status)

        openSearch.setOnClickListener { show("Search", input, submit) }
        submit.setOnClickListener {
            val term = input.text.toString().ifBlank { "Demo" }
            result.text = "$term: Episode 1"
            show("Results", result, play)
        }
        result.setOnClickListener { play.requestFocus() }
        play.setOnClickListener {
            player.text = "Playing: ${result.text}"
            show("Player", player)
        }
        show("Home", openSearch)
    }

    @Deprecated("Activity back handling")
    override fun onBackPressed() {
        if (title.text == "Home") super.onBackPressed() else show("Home", openSearch)
    }

    private fun show(screen: String, vararg visible: View) {
        title.text = screen
        listOf(openSearch, input, submit, result, play, player).forEach {
            it.visibility = if (it in visible) View.VISIBLE else View.GONE
        }
        visible.first().requestFocus()
    }
}
