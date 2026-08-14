package com.afif.expensetracker.ui.theme

import android.app.Activity
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.ColorScheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Composable
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.core.view.WindowCompat

enum class LedgerThemePalette(
    val storageValue: String,
    val label: String,
    val previewPrimary: Color,
    val previewSecondary: Color,
) {
    DARK_GREEN("dark_green", "Dark green", Color(0xFF65E6A3), Color(0xFFB6A4FF)),
    BLUE("blue", "Blue", Color(0xFF78ABFF), Color(0xFF7ED9F5)),
    DARK_BLUE("dark_blue", "Dark blue", Color(0xFF91B7FF), Color(0xFFB8A6FF));

    companion object {
        fun fromStorageValue(value: String?): LedgerThemePalette =
            entries.firstOrNull { it.storageValue == value } ?: DARK_GREEN
    }
}

private data class LedgerSemanticColors(
    val expense: Color,
    val warning: Color,
    val income: Color,
    val chartAccent: Color,
)

private data class LedgerPaletteSpec(
    val colorScheme: ColorScheme,
    val semanticColors: LedgerSemanticColors,
)

private val DarkGreenInk = Color(0xFF08110F)
private val DarkGreenSurface = Color(0xFF101B17)
private val DarkGreenElevated = Color(0xFF182720)
private val DarkGreenMint = Color(0xFF65E6A3)
private val DarkGreenExpense = Color(0xFFFF869A)
private val DarkGreenWarning = Color(0xFFF6CF69)
private val DarkGreenIncome = Color(0xFF63D8CE)
private val DarkGreenChartAccent = Color(0xFFB6A4FF)

private val DarkGreenColors = darkColorScheme(
    primary = DarkGreenMint,
    onPrimary = Color(0xFF062117),
    primaryContainer = Color(0xFF1D5740),
    onPrimaryContainer = Color(0xFFB9F5D2),
    inversePrimary = Color(0xFF226B4B),
    secondary = Color(0xFFA7CDBB),
    onSecondary = Color(0xFF10271D),
    secondaryContainer = Color(0xFF29483A),
    onSecondaryContainer = Color(0xFFC5EBD7),
    tertiary = DarkGreenChartAccent,
    onTertiary = Color(0xFF21164D),
    tertiaryContainer = Color(0xFF42376D),
    onTertiaryContainer = Color(0xFFE9DEFF),
    background = DarkGreenInk,
    onBackground = Color(0xFFE9F4EE),
    surface = DarkGreenSurface,
    onSurface = Color(0xFFE9F4EE),
    surfaceVariant = Color(0xFF24332C),
    onSurfaceVariant = Color(0xFFB5C5BB),
    surfaceTint = DarkGreenMint,
    inverseSurface = Color(0xFFE9F4EE),
    inverseOnSurface = Color(0xFF1A211D),
    error = DarkGreenExpense,
    onError = Color(0xFF3B0716),
    errorContainer = Color(0xFF6B2033),
    onErrorContainer = Color(0xFFFFD9DE),
    outline = Color(0xFF7F9186),
    outlineVariant = Color(0xFF3B4B43),
    scrim = Color.Black,
    surfaceDim = Color(0xFF0D1713),
    surfaceBright = Color(0xFF25352D),
    surfaceContainerLowest = Color(0xFF050B09),
    surfaceContainerLow = Color(0xFF121F1A),
    surfaceContainer = DarkGreenElevated,
    surfaceContainerHigh = Color(0xFF1E2D25),
    surfaceContainerHighest = Color(0xFF293930),
)

private val BlueColors = darkColorScheme(
    primary = Color(0xFF78ABFF),
    onPrimary = Color(0xFF002A5C),
    primaryContainer = Color(0xFF214D80),
    onPrimaryContainer = Color(0xFFD6E5FF),
    inversePrimary = Color(0xFF285F9E),
    secondary = Color(0xFFAEC9EF),
    onSecondary = Color(0xFF102A46),
    secondaryContainer = Color(0xFF2A4563),
    onSecondaryContainer = Color(0xFFD5E5FF),
    tertiary = Color(0xFF7ED9F5),
    onTertiary = Color(0xFF003641),
    tertiaryContainer = Color(0xFF155365),
    onTertiaryContainer = Color(0xFFB8EFFF),
    background = Color(0xFF0D1726),
    onBackground = Color(0xFFEAF1FF),
    surface = Color(0xFF142136),
    onSurface = Color(0xFFEAF1FF),
    surfaceVariant = Color(0xFF24364D),
    onSurfaceVariant = Color(0xFFB8C7DC),
    surfaceTint = Color(0xFF78ABFF),
    inverseSurface = Color(0xFFEAF1FF),
    inverseOnSurface = Color(0xFF17202D),
    error = Color(0xFFFF8EA1),
    onError = Color(0xFF3B0716),
    errorContainer = Color(0xFF6A2034),
    onErrorContainer = Color(0xFFFFD9DE),
    outline = Color(0xFF8292A8),
    outlineVariant = Color(0xFF3B4C63),
    scrim = Color.Black,
    surfaceDim = Color(0xFF0A1422),
    surfaceBright = Color(0xFF263850),
    surfaceContainerLowest = Color(0xFF070E19),
    surfaceContainerLow = Color(0xFF111D2F),
    surfaceContainer = Color(0xFF1A2A41),
    surfaceContainerHigh = Color(0xFF213249),
    surfaceContainerHighest = Color(0xFF293A52),
)

private val DarkBlueColors = darkColorScheme(
    primary = Color(0xFF91B7FF),
    onPrimary = Color(0xFF002B63),
    primaryContainer = Color(0xFF183F75),
    onPrimaryContainer = Color(0xFFD8E4FF),
    inversePrimary = Color(0xFF3567A5),
    secondary = Color(0xFFB6C8E8),
    onSecondary = Color(0xFF1C2D46),
    secondaryContainer = Color(0xFF30415B),
    onSecondaryContainer = Color(0xFFD8E3FA),
    tertiary = Color(0xFFB8A6FF),
    onTertiary = Color(0xFF28185E),
    tertiaryContainer = Color(0xFF43357A),
    onTertiaryContainer = Color(0xFFE8DEFF),
    background = Color(0xFF050A14),
    onBackground = Color(0xFFE9EFFF),
    surface = Color(0xFF0A1324),
    onSurface = Color(0xFFE9EFFF),
    surfaceVariant = Color(0xFF1D2A40),
    onSurfaceVariant = Color(0xFFB5C2D8),
    surfaceTint = Color(0xFF91B7FF),
    inverseSurface = Color(0xFFE9EFFF),
    inverseOnSurface = Color(0xFF151D2B),
    error = Color(0xFFFF879B),
    onError = Color(0xFF3B0716),
    errorContainer = Color(0xFF671F32),
    onErrorContainer = Color(0xFFFFD9DE),
    outline = Color(0xFF7F8DA5),
    outlineVariant = Color(0xFF344158),
    scrim = Color.Black,
    surfaceDim = Color(0xFF040812),
    surfaceBright = Color(0xFF202D43),
    surfaceContainerLowest = Color(0xFF02050C),
    surfaceContainerLow = Color(0xFF08101F),
    surfaceContainer = Color(0xFF111D32),
    surfaceContainerHigh = Color(0xFF18253A),
    surfaceContainerHighest = Color(0xFF202D43),
)

private val DarkGreenSemantics = LedgerSemanticColors(
    expense = DarkGreenExpense,
    warning = DarkGreenWarning,
    income = DarkGreenIncome,
    chartAccent = DarkGreenChartAccent,
)
private val LocalLedgerSemanticColors = staticCompositionLocalOf { DarkGreenSemantics }

private fun LedgerThemePalette.spec(): LedgerPaletteSpec = when (this) {
    LedgerThemePalette.DARK_GREEN -> LedgerPaletteSpec(DarkGreenColors, DarkGreenSemantics)
    LedgerThemePalette.BLUE -> LedgerPaletteSpec(
        BlueColors,
        LedgerSemanticColors(
            expense = Color(0xFFFF8EA1),
            warning = Color(0xFFF4CB72),
            income = Color(0xFF68D8CE),
            chartAccent = Color(0xFF8EBBFF),
        ),
    )
    LedgerThemePalette.DARK_BLUE -> LedgerPaletteSpec(
        DarkBlueColors,
        LedgerSemanticColors(
            expense = Color(0xFFFF879B),
            warning = Color(0xFFFFD166),
            income = Color(0xFF58D5C9),
            chartAccent = Color(0xFFA78BFA),
        ),
    )
}

val Ink: Color
    @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.background
val Surface: Color
    @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.surface
val Elevated: Color
    @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.surfaceContainer
val Mint: Color
    @Composable @ReadOnlyComposable get() = MaterialTheme.colorScheme.primary
val Expense: Color
    @Composable @ReadOnlyComposable get() = LocalLedgerSemanticColors.current.expense
val Warning: Color
    @Composable @ReadOnlyComposable get() = LocalLedgerSemanticColors.current.warning
val Income: Color
    @Composable @ReadOnlyComposable get() = LocalLedgerSemanticColors.current.income
val ChartAccent: Color
    @Composable @ReadOnlyComposable get() = LocalLedgerSemanticColors.current.chartAccent

/** Typography keeps amounts aligned and easy to scan with tabular numerals. */
val LedgerTypography = Typography(
    displayLarge = TextStyle(
        fontSize = 36.sp,
        lineHeight = 44.sp,
        fontWeight = FontWeight.Bold,
        letterSpacing = (-0.5).sp,
        fontFeatureSettings = "tnum",
    ),
    displayMedium = TextStyle(
        fontSize = 30.sp,
        lineHeight = 38.sp,
        fontWeight = FontWeight.Bold,
        fontFeatureSettings = "tnum",
    ),
    displaySmall = TextStyle(
        fontSize = 32.sp,
        lineHeight = 38.sp,
        fontWeight = FontWeight.Bold,
        letterSpacing = (-0.4).sp,
        fontFeatureSettings = "tnum",
    ),
    headlineLarge = TextStyle(fontSize = 28.sp, lineHeight = 36.sp, fontWeight = FontWeight.Bold),
    headlineMedium = TextStyle(
        fontSize = 25.sp,
        lineHeight = 32.sp,
        fontWeight = FontWeight.Bold,
        letterSpacing = (-0.2).sp,
    ),
    headlineSmall = TextStyle(fontSize = 21.sp, lineHeight = 28.sp, fontWeight = FontWeight.SemiBold),
    titleLarge = TextStyle(fontSize = 20.sp, lineHeight = 26.sp, fontWeight = FontWeight.SemiBold),
    titleMedium = TextStyle(
        fontSize = 16.sp,
        lineHeight = 24.sp,
        fontWeight = FontWeight.SemiBold,
        fontFeatureSettings = "tnum",
    ),
    titleSmall = TextStyle(fontSize = 14.sp, lineHeight = 20.sp, fontWeight = FontWeight.SemiBold),
    bodyLarge = TextStyle(fontSize = 16.sp, lineHeight = 24.sp, fontFeatureSettings = "tnum"),
    bodyMedium = TextStyle(fontSize = 14.sp, lineHeight = 20.sp, fontFeatureSettings = "tnum"),
    bodySmall = TextStyle(fontSize = 12.sp, lineHeight = 16.sp),
    labelLarge = TextStyle(fontSize = 14.sp, lineHeight = 20.sp, fontWeight = FontWeight.SemiBold),
    labelMedium = TextStyle(fontSize = 12.sp, lineHeight = 16.sp, fontWeight = FontWeight.Medium),
    labelSmall = TextStyle(fontSize = 11.sp, lineHeight = 16.sp, fontWeight = FontWeight.Medium),
)

val LedgerShapes = Shapes(
    extraSmall = RoundedCornerShape(8.dp),
    small = RoundedCornerShape(12.dp),
    medium = RoundedCornerShape(18.dp),
    large = RoundedCornerShape(24.dp),
    extraLarge = RoundedCornerShape(30.dp),
)

@Composable
fun LedgerTheme(
    palette: LedgerThemePalette = LedgerThemePalette.DARK_GREEN,
    content: @Composable () -> Unit,
) {
    val spec = palette.spec()
    val view = LocalView.current
    SideEffect {
        val window = (view.context as? Activity)?.window ?: return@SideEffect
        val systemBarColor = spec.colorScheme.surfaceContainerLowest.toArgb()
        window.statusBarColor = systemBarColor
        window.navigationBarColor = systemBarColor
        WindowCompat.getInsetsController(window, view).apply {
            isAppearanceLightStatusBars = false
            isAppearanceLightNavigationBars = false
        }
    }
    CompositionLocalProvider(LocalLedgerSemanticColors provides spec.semanticColors) {
        MaterialTheme(
            colorScheme = spec.colorScheme,
            typography = LedgerTypography,
            shapes = LedgerShapes,
            content = content,
        )
    }
}
